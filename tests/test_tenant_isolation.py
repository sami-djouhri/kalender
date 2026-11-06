"""Tests fuer das Tenant-Scoping (backend/tenant.py): Basis-Isolation.

GELTUNGSBEREICH (ehrlich): sichert ab, dass das ORM-Tenant-Scoping aktiv ist und
fremde subs 0 STRICT-Zeilen sehen (faengt Entfernen/Brechen des Scopings).
Reproduziert NICHT die Cache-Poisoning-Variante des 2026-07-06-Bugs
(with_loader_criteria ohne Lambda), die entsteht nur im langlebigen Prozess mit
warmem Statement-Cache und ist live verifiziert (fremder sub -> 0 nach Deploy).

unittest-Style (Projekt nutzt `python -m unittest discover -s tests`).
"""

import unittest

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.tenant as tenant
from backend.database import Base
from backend.models import Contact


def _make_sessionmaker():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    sess = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    event.listen(sess, "do_orm_execute", tenant._apply_tenant_scope)
    event.listen(sess, "before_flush", tenant._stamp_tenant)
    return sess


def _count(sess_factory, sub):
    s = sess_factory()
    s.info["owner_sub"] = sub
    try:
        return s.scalar(select(func.count()).select_from(Contact))
    finally:
        s.close()


class TenantIsolationTest(unittest.TestCase):
    def test_foreign_sub_sees_nothing(self):
        Sess = _make_sessionmaker()
        seed = Sess()
        seed.info["owner_sub"] = "ownerA"
        seed.add(Contact(name="Alice"))
        seed.add(Contact(name="Bob"))
        seed.commit()
        seed.close()

        self.assertEqual(_count(Sess, "ownerA"), 2)
        self.assertEqual(_count(Sess, "FOREIGN-XYZ"), 0)
        self.assertEqual(_count(Sess, "ownerA"), 2)

    def test_before_flush_stamps_owner_sub(self):
        Sess = _make_sessionmaker()
        s = Sess()
        s.info["owner_sub"] = "ownerB"
        c = Contact(name="Carol")
        s.add(c)
        s.commit()
        self.assertEqual(c.owner_sub, "ownerB")
        s.close()


if __name__ == "__main__":
    unittest.main()
