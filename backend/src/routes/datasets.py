from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from src.config.database import get_db
from src.models import Dataset
from src.schemas.dataset import DatasetInput
from src.middlewares.auth import current_user, can_write
from src.services import datasets, material
from src.services.audit import record
from src.utils.serialization import dataset_dict
router=APIRouter(prefix='/datasets')


def _with_effective_recall(db,d,data):
    """Flag a batch covered by a recall whose scan has not paged through all segments yet."""
    if not data.get('recalled'):
        ticket=material.dataset_under_recall(db,d.id)
        if ticket:
            data=data.copy();data['recalled']=True;data['recall_ticket_id']=ticket;data['recall_status']='quarantined'
    return data
@router.get('')
def listing(db=Depends(get_db),u=Depends(current_user)):
    return [_with_effective_recall(db,d,dataset_dict(d)) for d in db.scalars(select(Dataset).order_by(Dataset.id.desc()))]
@router.post('',status_code=201)
def create(data:DatasetInput,db=Depends(get_db),u=Depends(can_write)):
    d=datasets.create(db,data); record(db,u.email,'dataset.create',{'id':d.id,'rows':len(d.records)})
    db.commit(); return dataset_dict(d)
@router.get('/{id}')
def detail(id:int,db=Depends(get_db),u=Depends(current_user)):
    d=db.get(Dataset,id)
    if not d:raise HTTPException(404,'数据批次不存在')
    return {**_with_effective_recall(db,d,dataset_dict(d)),'records':d.records}
