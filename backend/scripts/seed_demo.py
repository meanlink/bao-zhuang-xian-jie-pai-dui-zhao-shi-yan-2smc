import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, Roll, RecallTicket
from src.services.business import metadata, seed_records, analyze
from src.services import material
from src.services.reports import deliver
from src.utils.security import hash_password
from src.utils.serialization import checksum
def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        rows=seed_records()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
            material.index_dataset_runs(db, d)
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        _seed_material(db, d, rows)
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit,Roll,RecallTicket]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
def _seed_material(db, dataset, rows):
    """Small idempotent lineage demo: source roll split into two lots, then merged."""
    if db.scalar(select(Roll).limit(1)):
        return
    from src.services import material
    for code, name in [('YC-SRC-01', '试运行来源卷'), ('YC-LOT-A', '早班分切卷'), ('YC-LOT-B', '晚班分切卷'), ('YC-MIX-02', '混批合并卷')]:
        material.create_roll(db, code, name)
    material.link_rolls(db, 'YC-SRC-01', 'YC-LOT-A', 'split')
    material.link_rolls(db, 'YC-SRC-01', 'YC-LOT-B', 'split')
    material.link_rolls(db, 'YC-LOT-A', 'YC-MIX-02', 'merge')
    material.link_rolls(db, 'YC-LOT-B', 'YC-MIX-02', 'merge')
    runs=[r['run'] for r in rows]
    for run in runs[:4]: material.attach_usage(db, 'YC-LOT-A', run)
    for run in runs[4:8]: material.attach_usage(db, 'YC-LOT-B', run)
    for run in runs[8:12]: material.attach_usage(db, 'YC-MIX-02', run)
    db.flush()


if __name__=='__main__':seed()
