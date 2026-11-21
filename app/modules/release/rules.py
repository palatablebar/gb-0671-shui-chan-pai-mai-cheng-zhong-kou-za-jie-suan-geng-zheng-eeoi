"""日结放款：按买家信用额度覆盖未结票与更正敞口，只放行已核对票。

放款计划是输入快照与规则的纯函数：同一批重跑得到同一计划，不会重复占用额度。
未闭合更正、同票金额冲突、额度不足的票一律保留待处理，已核对票可先释放。
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from app.coerce import money
from app.errors import BusinessError

OPEN = 'CORRECTION_OPEN'
CONFLICT = 'TICKET_AMOUNT_CONFLICT'
VERSION_CONFLICT = 'VERSION_CONFLICT'
EXPOSURE_UNKNOWN = 'EXPOSURE_UNKNOWN'
OVER = 'OVER_LIMIT'
BUYER_OVER = 'BUYER_OVER_LIMIT'
UNMAPPED = 'UNMAPPED_BUYER'
UNMAPPED_BUYER_ID = '__UNMAPPED__'

REASON_TEXT = {
    OPEN: '更正未闭合，保留待处理',
    CONFLICT: '同票金额冲突，保留待处理',
    VERSION_CONFLICT: '更正版本链冲突，保留待处理',
    EXPOSURE_UNKNOWN: '更正金额未定，暂缓放款',
    OVER: '已核对票超出剩余额度，暂缓放款',
    BUYER_OVER: '买家累计欠款已超信用额度，暂缓放款',
    UNMAPPED: '票号未映射买家，保留待处理',
}


def _amount(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _daily(state):
    daily = (state.get('context') or {}).get('daily') or {}
    buyers = {}
    for buyer_id, cfg in (daily.get('buyers') or {}).items():
        limit = _amount((cfg or {}).get('credit_limit'))
        if limit is None or limit < 0:
            raise BusinessError('BAD_CONTEXT', f'买家 {buyer_id} 信用额度无效')
        buyers[str(buyer_id)] = {'name': str((cfg or {}).get('name') or buyer_id), 'credit_limit': limit}
    mapping = {str(k): str(v) for k, v in (daily.get('ticket_buyer') or {}).items()}
    carryover = []
    for item in daily.get('carryover') or []:
        amount = _amount((item or {}).get('amount'))
        if amount is None:
            raise BusinessError('BAD_CONTEXT', '未结票金额无效')
        carryover.append({'buyer': str(item.get('buyer')), 'ticket': str(item.get('ticket') or ''),
                          'amount': amount, 'source': str(item.get('source') or '前日未结')})
    notices = []
    for item in daily.get('corrections') or []:
        item = item or {}
        ticket = str(item.get('ticket') or '').strip()
        status = item.get('status')
        if not ticket or status not in {'open', 'closed'}:
            raise BusinessError('BAD_CONTEXT', '更正通知缺少票号或有效状态')
        raw = item.get('reported_amount')
        reported = None
        if raw not in (None, ''):
            reported = _amount(raw)
            if reported is None or reported < 0:
                raise BusinessError('BAD_CONTEXT', f'票 {ticket} 的更正金额无效')
        notices.append({'ticket': ticket, 'version': item.get('version'), 'status': status,
                        'reported_amount': reported, 'source': str(item.get('source') or '复秤更正')})
    return {'date': str(daily.get('date') or ''), 'buyers': buyers, 'mapping': mapping,
            'carryover': carryover, 'notices': notices}


def _evidence(state):
    """收集原始成交与全部计价版本，按票号+版本识别金额冲突。"""
    by_version = defaultdict(set)
    paid, max_amount, max_version = {}, {}, {}
    for row in state.get('settlements') or []:
        ticket, version = row['ticket'], int(row['version'])
        amount = Decimal(row['amount'])
        by_version[(ticket, version)].add(amount)
        paid[ticket] = max(paid.get(ticket, Decimal(0)), Decimal(row['paid']))
        max_amount[ticket] = max(max_amount.get(ticket, amount), amount)
        max_version[ticket] = max(max_version.get(ticket, version), version)
    return {'paid': paid, 'max_amount': max_amount, 'max_version': max_version,
            'amount_conflicts': sorted({t for (t, _), values in by_version.items() if len(values) > 1})}


def _released(row, buyer, version, reason=''):
    return {'buyer': buyer, 'ticket': row['ticket'], 'adopted_version': row['version'],
            'candidate_version': version, 'amount': row['amount'], 'paid': row['paid'],
            'due': row['due'], 'reserve': '0.00', 'reserve_unknown': False,
            'reason': reason, 'detail': REASON_TEXT.get(reason, ''), 'status': '可释放' if not reason else '暂缓'}


def build_plan(state):
    daily = _daily(state)
    evidence = _evidence(state)
    final_by_ticket = {}
    for row in state.get('final') or []:
        normalized = dict(row)
        if normalized.get('due') in (None, ''):
            normalized['due'] = money(Decimal(normalized['amount']) - Decimal(normalized['paid']))
        final_by_ticket[normalized['ticket']] = normalized

    blocked = {str(c.get('ticket')) for c in state.get('version_conflicts') or [] if c.get('ticket')}
    open_tickets, conflict_tickets = set(), set()
    notice_versions = {}
    notices_by_ticket = defaultdict(list)
    for notice in daily['notices']:
        ticket = notice['ticket']
        notices_by_ticket[ticket].append(notice)
        if notice['version'] is not None:
            try:
                notice_versions[ticket] = max(notice_versions.get(ticket, 0), int(notice['version']))
            except (TypeError, ValueError):
                pass
        if notice['status'] == 'open':
            open_tickets.add(ticket)
            continue
        effective = final_by_ticket.get(ticket)
        if effective is None:
            open_tickets.add(ticket)
        elif notice['reported_amount'] is None:
            # 已标闭合却没有金额，无法核对闭合，按未闭合暂缓该票
            open_tickets.add(ticket)
        elif notice['reported_amount'] != Decimal(effective['amount']):
            conflict_tickets.add(ticket)
    for ticket, items in notices_by_ticket.items():
        closed = {n['reported_amount'] for n in items
                  if n['status'] == 'closed' and n['reported_amount'] is not None}
        if len(closed) > 1:
            conflict_tickets.add(ticket)
    conflict_tickets |= set(evidence['amount_conflicts'])

    held_reason = {}
    for ticket in open_tickets:
        held_reason[ticket] = OPEN
    for ticket in conflict_tickets:
        held_reason[ticket] = CONFLICT
    for ticket in blocked:
        held_reason[ticket] = VERSION_CONFLICT
    # 已闭合且与生效金额一致的更正不阻断；仍开放或对不上的才会进入 held_reason。

    def owner(ticket):
        buyer_id = daily['mapping'].get(ticket)
        return buyer_id if buyer_id in daily['buyers'] else UNMAPPED_BUYER_ID

    def candidate_version(ticket):
        version = evidence['max_version'].get(ticket, notice_versions.get(ticket, ''))
        return version

    def reserve_for(ticket):
        """更正敞口：候选取最大金额，减去已付，至少为 0。无金额证据则未知。"""
        known = [evidence['max_amount'].get(ticket)]
        known += [n['reported_amount'] for n in notices_by_ticket.get(ticket, [])
                  if n['reported_amount'] is not None]
        known = [value for value in known if value is not None]
        if not known:
            return Decimal(0), True
        reserve = max(Decimal(0), max(known) - evidence['paid'].get(ticket, Decimal(0)))
        return reserve, False

    def held_row(ticket, reason, reserve=None, unknown=None):
        effective = final_by_ticket.get(ticket)
        if reserve is None or unknown is None:
            reserve, unknown = reserve_for(ticket)
        return {'buyer': owner(ticket), 'ticket': ticket, 'reason': reason,
                'detail': REASON_TEXT[reason], 'status': '暂缓',
                'adopted_version': effective['version'] if effective else '',
                'candidate_version': candidate_version(ticket),
                'amount': effective['amount'] if effective else (
                    money(evidence['max_amount'][ticket]) if ticket in evidence['max_amount'] else ''),
                'paid': effective['paid'] if effective else (
                    money(evidence['paid'][ticket]) if ticket in evidence['paid'] else ''),
                'due': effective['due'] if effective else '',
                'reserve': money(reserve), 'reserve_unknown': bool(unknown)}

    groups = {bid: {'released': [], 'held': [], 'carryover': []} for bid in daily['buyers']}
    groups[UNMAPPED_BUYER_ID] = {'released': [], 'held': [], 'carryover': []}

    for item in daily['carryover']:
        bid = item['buyer'] if item['buyer'] in daily['buyers'] else UNMAPPED_BUYER_ID
        groups[bid]['carryover'].append({'buyer': bid, 'ticket': item['ticket'],
                                         'amount': money(item['amount']), 'source': item['source']})

    candidate_tickets = sorted(t for t in final_by_ticket if t not in held_reason)
    # 未映射买家的票没有可用额度，全部保留待处理，不能静默放款
    for ticket in candidate_tickets:
        if owner(ticket) == UNMAPPED_BUYER_ID:
            held_reason[ticket] = UNMAPPED
    candidate_tickets = [t for t in candidate_tickets if t not in held_reason]

    for ticket in sorted(held_reason):
        row = held_row(ticket, held_reason[ticket])
        row['buyer'] = owner(ticket)
        groups[owner(ticket)]['held'].append(row)

    summaries = []
    for bid in sorted(daily['buyers']):
        cfg = daily['buyers'][bid]
        group = groups[bid]
        carry = sum((Decimal(r['amount']) for r in group['carryover']), Decimal(0))
        held = list(group['held'])
        reserves = sum((Decimal(r['reserve']) for r in held), Decimal(0))
        uncertain = any(r['reserve_unknown'] for r in held)
        base = carry + reserves
        available = cfg['credit_limit'] - base
        over_buyer = base > cfg['credit_limit'] or available < 0
        released = []
        own = sorted((final_by_ticket[t] for t in candidate_tickets if owner(t) == bid),
                     key=lambda r: r['ticket'])
        # 退款/零待付先放行，它们只会释放额度
        for row in [r for r in own if Decimal(r['due']) <= 0]:
            released.append(_released(row, bid, candidate_version(row['ticket'])))
        for row in [r for r in own if Decimal(r['due']) > 0]:
            due = Decimal(row['due'])
            if uncertain:
                # 存在金额未定的更正，无法保证累计欠款不越额度，先暂缓该买家其余欠款票
                held.append(_released(row, bid, candidate_version(row['ticket']), EXPOSURE_UNKNOWN))
            elif over_buyer:
                held.append(_released(row, bid, candidate_version(row['ticket']), BUYER_OVER))
            elif available >= due:
                released.append(_released(row, bid, candidate_version(row['ticket'])))
                available -= due
            else:
                held.append(_released(row, bid, candidate_version(row['ticket']), OVER))
        released_due = sum((Decimal(r['due']) for r in released), Decimal(0))
        occupied = base + released_due
        headroom = cfg['credit_limit'] - occupied
        if occupied > cfg['credit_limit']:
            status = '额度已超'
        elif held:
            status = '部分放行' if released else '待处理'
        else:
            status = '全部放行'
        summaries.append({'buyer': bid, 'name': cfg['name'], 'credit_limit': money(cfg['credit_limit']),
                          'carryover': money(carry), 'reserved': money(reserves),
                          'released_due': money(released_due), 'occupied': money(occupied),
                          'headroom': money(headroom), 'status': status,
                          'ticket_count': len(released) + len(held),
                          'released_count': len(released), 'held_count': len(held),
                          'released': released, 'held': held, 'carryover_rows': group['carryover']})

    unmapped = groups[UNMAPPED_BUYER_ID]
    if unmapped['released'] or unmapped['held'] or unmapped['carryover']:
        for row in unmapped['released']:
            row.update(reason=UNMAPPED, detail=REASON_TEXT[UNMAPPED], status='暂缓')
            unmapped['held'].append(row)
        unmapped['released'] = []
        carry = sum((Decimal(r['amount']) for r in unmapped['carryover']), Decimal(0))
        reserves = sum((Decimal(r['reserve']) for r in unmapped['held']), Decimal(0))
        summaries.append({'buyer': UNMAPPED_BUYER_ID, 'name': '未映射买家', 'credit_limit': '0.00',
                          'carryover': money(carry), 'reserved': money(reserves),
                          'released_due': '0.00', 'occupied': money(carry + reserves), 'headroom': '0.00',
                          'status': '待处理', 'ticket_count': len(unmapped['held']),
                          'released_count': 0, 'held_count': len(unmapped['held']),
                          'released': [], 'held': unmapped['held'],
                          'carryover_rows': unmapped['carryover']})

    all_released = [row for summary in summaries for row in summary['released']]
    all_held = [row for summary in summaries for row in summary['held']]
    statuses = {summary['status'] for summary in summaries}
    if '额度已超' in statuses:
        plan_status = '额度已超'
    elif all_held:
        plan_status = '部分暂缓'
    else:
        plan_status = '可发布'
    return {
        'date': daily['date'], 'status': plan_status, 'buyers': summaries,
        'released_count': len(all_released), 'held_count': len(all_held),
        'released_due': money(sum((Decimal(r['due']) for r in all_released), Decimal(0))),
        'reserved_total': money(sum((Decimal(r['reserve']) for r in all_held), Decimal(0))),
        'credit_total': money(sum((cfg['credit_limit'] for cfg in daily['buyers'].values()), Decimal(0))),
        'evidence': {
            'open_corrections': sorted(open_tickets),
            'amount_conflicts': sorted(conflict_tickets),
            'held_reasons': {ticket: reason for ticket, reason in sorted(held_reason.items())},
            'version_conflicts': list(state.get('version_conflicts') or []),
            'notices': [{'ticket': n['ticket'], 'version': n['version'], 'status': n['status'],
                         'reported_amount': money(n['reported_amount']) if n['reported_amount'] is not None else '',
                         'source': n['source']} for n in daily['notices']],
            'adopted_versions': [{'ticket': row['ticket'], 'version': row['version'],
                                  'paid': row['paid'], 'amount': row['amount'], 'due': row['due']}
                                 for row in sorted(final_by_ticket.values(), key=lambda r: r['ticket'])],
        },
    }


def apply(state):
    plan = build_plan(state)
    state['release'] = plan
    state['views']['release'] = {
        'title': '日结放款', 'rows': plan['buyers'],
        'metrics': {'买家': len(plan['buyers']), '可释放票': plan['released_count'],
                    '暂缓票': plan['held_count'], '预留敞口_CNY': plan['reserved_total'],
                    '状态': plan['status']},
    }
