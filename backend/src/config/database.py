from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .settings import DATABASE_URL

class Base(DeclarativeBase):
    pass

_engine_args = {'pool_pre_ping': True, 'pool_recycle': 1800}
if DATABASE_URL.startswith('sqlite'):
    # Local dev/testing only: let concurrent writers wait for the recall gate instead of
    # failing immediately, and share one connection pool across request threads.
    _engine_args.update(connect_args={'timeout': 30, 'check_same_thread': False})

engine = create_engine(DATABASE_URL, **_engine_args)
Session = sessionmaker(engine, expire_on_commit=False)

def get_db():
    with Session() as db:
        yield db
