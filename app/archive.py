import io, csv, json, zipfile, hashlib
from app.errors import BusinessError

def serialize(result):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')},ensure_ascii=False,indent=2))
        evidence={'counts':result.get('counts',{}),'views':result['views'],'quarantine':result.get('quarantine',[]),
                  'release':result.get('release'),'input_snapshot':result.get('input_snapshot')}
        archive.writestr('evidence.json',json.dumps(evidence,ensure_ascii=False,indent=2))
        if result.get('published_release'):
            archive.writestr('release_published.json',json.dumps(result['published_release'],ensure_ascii=False,indent=2))
        if result.get('release_history'):
            archive.writestr('release_versions.json',json.dumps(result['release_history'],ensure_ascii=False,indent=2))
    return buffer.getvalue()
