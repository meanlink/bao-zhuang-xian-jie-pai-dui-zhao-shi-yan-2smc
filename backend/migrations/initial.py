from sqlalchemy import inspect, text, select
from src.config.database import Base, Session, engine
from src import models


def migrate():
    Base.metadata.create_all(engine)
    _seed_gate()
    _extend_existing_tables()
    _backfill_dataset_runs()


def _seed_gate():
    with Session() as db:
        gate = db.get(models.MaterialGate, 1)
        if not gate:
            db.add(models.MaterialGate(id=1, version=0))
            db.commit()


def _extend_existing_tables():
    """Additive columns from the material-lineage release; safe to run repeatedly."""
    additions = {
        'datasets': {
            'recall_ticket_id': 'INTEGER NULL',
            'recall_status': "VARCHAR(20) NOT NULL DEFAULT ''",
        },
        'jobs': {
            'recall_ticket_id': 'INTEGER NULL',
            'recall_status': "VARCHAR(20) NOT NULL DEFAULT ''",
        },
        'reports': {
            'recall_ticket_id': 'INTEGER NULL',
            'recall_status': "VARCHAR(20) NOT NULL DEFAULT ''",
        },
    }
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, columns in additions.items():
            if table not in inspector.get_table_names():
                continue
            existing = {c['name'] for c in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))


def _backfill_dataset_runs():
    """Datasets imported before this release get their run index on first boot."""
    with Session() as db:
        indexed = {r[0] for r in db.execute(text('SELECT DISTINCT dataset_id FROM dataset_runs'))}
        for d in db.scalars(select(models.Dataset).order_by(models.Dataset.id)):
            if d.id in indexed:
                continue
            seen = set()
            for row in d.records or []:
                run = str(row.get('run', '')).strip()
                if not run or run in seen:
                    continue
                seen.add(run)
                db.add(models.DatasetRun(dataset_id=d.id, run=run, shift=str(row.get('shift', ''))))
        db.commit()


if __name__ == '__main__':
    migrate()
