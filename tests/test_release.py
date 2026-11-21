import io, json, zipfile
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import main, store
from app.engine import evaluate
from app.errors import BusinessError
from app.modules.release.rules import build_plan


@pytest.fixture
def release():
    return evaluate({'dataset': 'release'})


def buyer(plan, buyer_id):
    return next(row for row in plan['buyers'] if row['buyer'] == buyer_id)


def ticket_rows(summary):
    return {row['ticket']: row for row in summary['released'] + summary['held']}


def test_release_plan_real(release):
    plan = release['release']
    assert plan['status'] == '额度已超'
    assert plan['released_count'] == 4 and plan['held_count'] == 4


def test_release_occupied_covers_carryover_and_reserve(release):
    plan = release['release']
    b01, b02 = buyer(plan, 'B01'), buyer(plan, 'B02')
    assert b01['carryover'] == '0.00'
    assert b01['reserved'] == '902.20'
    assert b01['released_due'] == '3739.20'
    assert b01['occupied'] == '4641.40'
    assert b01['headroom'] == '1858.60'
    assert b02['carryover'] == '1200.00'
    assert b02['reserved'] == '2485.35'
    assert b02['occupied'] == '4970.70'


def test_release_occupancy_within_limit(release):
    for summary in release['release']['buyers']:
        if summary['buyer'] == '__UNMAPPED__':
            continue
        occupied = sum(Decimal(summary[key]) for key in ('carryover', 'reserved', 'released_due'))
        assert occupied == Decimal(summary['occupied'])
        if summary['status'] != '额度已超':
            assert occupied <= Decimal(summary['credit_limit'])


def test_release_open_correction_held(release):
    rows = ticket_rows(buyer(release['release'], 'B02'))
    assert rows['01005']['reason'] == 'CORRECTION_OPEN'
    assert rows['01005']['reserve'] == '1285.35'
    assert rows['01005']['adopted_version'] == 1


def test_release_amount_conflict_held(release):
    rows = ticket_rows(buyer(release['release'], 'B02'))
    assert rows['01004']['reason'] == 'TICKET_AMOUNT_CONFLICT'
    assert rows['01004']['amount'] == '1168.50'
    assert rows['01004']['reserve'] == '1200.00'


def test_release_version_conflict_held(release):
    plan = release['release']
    rows = ticket_rows(buyer(plan, 'B01'))
    assert rows['01002']['reason'] == 'VERSION_CONFLICT'
    assert rows['01002']['candidate_version'] == 3
    assert rows['01002']['adopted_version'] == ''
    assert plan['evidence']['version_conflicts'][0]['code'] == 'STALE_CORRECTION'


def test_release_closed_correction_adopted(release):
    rows = ticket_rows(buyer(release['release'], 'B01'))
    assert rows['01008']['status'] == '可释放'
    assert rows['01008']['adopted_version'] == rows['01008']['candidate_version'] == 3


def test_release_buyer_over_limit_holds(release):
    summary = buyer(release['release'], 'B03')
    assert summary['status'] == '额度已超'
    assert summary['released'] == []
    assert summary['held'][0]['reason'] == 'BUYER_OVER_LIMIT'
    assert summary['headroom'] == '-1000.00'


def test_release_is_deterministic(release):
    again = evaluate({'dataset': 'release'})['release']
    assert release['release'] == again
    assert build_plan(release) == release['release']


def test_release_refund_released_before_charge(clean):
    # 每票只取一个版本；00001 已多付（待付为负）先放行，00002 欠款在额度 0 时暂缓
    rows = [dict(row) for row in clean['settlements']
            if row['ticket'] in {'00001', '00002'} and row['version'] == 1]
    final = [dict(row, code='ORIGINAL') for row in rows]
    final[0]['paid'] = str(Decimal(final[0]['amount']) + 10)
    final[0].pop('due', None)
    state = {'context': {'daily': {'buyers': {'B': {'credit_limit': '0'}},
                                   'ticket_buyer': {'00001': 'B', '00002': 'B'},
                                   'carryover': [], 'corrections': []}},
             'settlements': rows, 'final': final, 'version_conflicts': []}
    summary = build_plan(state)['buyers'][0]
    assert [row['ticket'] for row in summary['released']] == ['00001']
    assert summary['held'][0]['ticket'] == '00002'
    assert summary['held'][0]['reason'] == 'OVER_LIMIT'


def test_release_unknown_exposure_holds_buyer(release):
    context = json.loads(json.dumps(release['context']))
    context['daily']['buyers']['B02']['credit_limit'] = '99999'
    context['daily']['ticket_buyer']['01099'] = 'B02'
    # 只有更正通知、没有成交金额证据的票，敞口无法估算
    context['daily']['corrections'].append({'ticket': '01099', 'status': 'open', 'version': 1})
    plan = build_plan(dict(release, context=context))
    rows = ticket_rows(buyer(plan, 'B02'))
    assert rows['01099']['reason'] == 'CORRECTION_OPEN'
    assert rows['01099']['reserve_unknown'] is True
    # 敞口未知时同买家其余欠款票也不再放行，避免把累计欠款推过额度
    assert not buyer(plan, 'B02')['released']
    assert rows['01007']['reason'] == 'EXPOSURE_UNKNOWN'


def test_release_unmapped_ticket_held(clean):
    context = json.loads(json.dumps(clean['context']))
    context['daily'] = {'buyers': {'B': {'credit_limit': '99999'}}, 'ticket_buyer': {},
                        'carryover': [], 'corrections': []}
    unmapped = buyer(build_plan(dict(clean, context=context)), '__UNMAPPED__')
    assert unmapped['status'] == '待处理'
    assert unmapped['released_count'] == 0
    assert all(row['reason'] == 'UNMAPPED_BUYER' for row in unmapped['held'])


def test_release_bad_context_rejected(release):
    context = json.loads(json.dumps(release['context']))
    context['daily']['buyers']['B01']['credit_limit'] = 'abc'
    with pytest.raises(BusinessError):
        build_plan(dict(release, context=context))


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'release.sqlite'))
    return TestClient(main.app)


def test_release_api_and_page(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'release'}).json()
    assert c.post('/api/release', json={'dataset': 'release'}).status_code == 200
    html = c.get('/release?run_id=' + run['manifest']['run_id']).text
    for needle in ('已占额度 CNY', '可释放票', '暂缓票', '采用版本',
                   'CORRECTION_OPEN', 'TICKET_AMOUNT_CONFLICT', '01008'):
        assert needle in html


def test_release_publish_idempotent(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'release'}).json()['manifest']['run_id']
    first = c.post(f'/api/runs/{rid}/release', json={'revision': 1})
    assert first.status_code == 200 and first.json()['duplicate'] is False
    again = c.post(f'/api/runs/{rid}/release', json={'revision': first.json()['revision']})
    assert again.status_code == 200
    assert again.json()['duplicate'] is True
    assert again.json()['release_version'] == first.json()['release_version']
    assert again.json()['release_key'] == first.json()['release_key']
    assert len(c.get(f'/api/runs/{rid}').json()['release_history']) == 1


def test_release_rerun_same_key_no_double_occupancy(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'release'}).json()['manifest']['run_id']
    assert c.post(f'/api/runs/{rid}/rerun', json={}).status_code == 200
    first = c.post(f'/api/runs/{rid}/release', json={'revision': 1}).json()
    rerun = c.post('/api/runs', json={'dataset': 'release'}).json()
    assert rerun['manifest']['run_id'] == rid
    second = c.post(f'/api/runs/{rid}/release', json={'revision': first['revision']}).json()
    assert second['release_key'] == first['release_key'] and second['duplicate'] is True
    assert len(c.get(f'/api/runs/{rid}').json()['release_history']) == 1


def test_release_stale_revision_rejected(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'release'}).json()['manifest']['run_id']
    assert c.post(f'/api/runs/{rid}/release', json={'revision': 1}).status_code == 200
    assert c.post(f'/api/runs/{rid}/release', json={'revision': 1}).status_code == 409
    assert len(c.get(f'/api/runs/{rid}').json()['release_history']) == 1


def test_release_failure_keeps_previous_version(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'release'}).json()['manifest']['run_id']
    first = c.post(f'/api/runs/{rid}/release', json={'revision': 1}).json()

    original = store.release_key

    def broken(*args, **kwargs):
        raise RuntimeError('发布中断')
    monkeypatch.setattr(store, 'release_key', broken)
    with pytest.raises(RuntimeError):
        c.post(f'/api/runs/{rid}/release', json={'revision': first['revision']})
    monkeypatch.setattr(store, 'release_key', original)
    stored = c.get(f'/api/runs/{rid}').json()
    assert len(stored['release_history']) == 1
    assert stored['published_release']['release_key'] == first['release_key']
    assert stored['release']['released_count'] == 4


def test_release_mid_publish_failure_rolls_back(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'release'}).json()['manifest']['run_id']
    first = c.post(f'/api/runs/{rid}/release', json={'revision': 1}).json()

    real_connect = store.connect

    class Failing:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *args):
            if sql.lstrip().upper().startswith('UPDATE RUNS'):
                raise RuntimeError('发布中断')
            return self._con.execute(sql, *args)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._con.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self._con, name)

    monkeypatch.setattr(store, 'connect', lambda path: Failing(real_connect(path)))
    with pytest.raises(RuntimeError):
        store.publish(main.DB_PATH, rid, first['revision'])
    monkeypatch.setattr(store, 'connect', real_connect)
    stored = c.get(f'/api/runs/{rid}').json()
    assert len(stored['release_history']) == 1
    assert stored['published_release']['release_key'] == first['release_key']


def test_release_new_plan_version_keeps_old(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'release'}).json()['manifest']['run_id']
    first = c.post(f'/api/runs/{rid}/release', json={'revision': 1}).json()
    # 直接改写持久化计划，模拟规则修订后的新结果；旧版次必须保留
    path = main.DB_PATH
    with store.connect(path) as con:
        row = con.execute('SELECT result FROM runs WHERE id=?', (rid,)).fetchone()
        result = json.loads(row['result'])
        result['release']['status'] = '可发布'
        con.execute('UPDATE runs SET result=? WHERE id=?', (json.dumps(result, ensure_ascii=False), rid))
    second = c.post(f'/api/runs/{rid}/release', json={'revision': first['revision']}).json()
    assert second['duplicate'] is False and second['release_version'] == 2
    history = c.get(f'/api/runs/{rid}').json()['release_history']
    assert [entry['version'] for entry in history] == [1, 2]
    assert history[0]['release_key'] == first['release_key']
    assert history[0]['plan']['status'] == '额度已超'


def test_release_evidence_exported_intact(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'release'}).json()['manifest']['run_id']
    c.post(f'/api/runs/{rid}/release', json={'revision': 1})
    archive = zipfile.ZipFile(io.BytesIO(c.get('/api/export/' + rid).content))
    assert {'manifest.json', 'evidence.json', 'release_published.json',
            'release_versions.json'} <= set(archive.namelist())
    evidence = json.loads(archive.read('evidence.json'))
    assert evidence['release']['released_count'] == 4
    assert evidence['input_snapshot']['bytes_b64']
    assert json.loads(archive.read('release_published.json'))['version'] == 1
