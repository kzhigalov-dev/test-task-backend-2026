"""Atomic batch synchronization of CRM entities into abstract customers."""
import json
from collections import defaultdict

from sqlalchemy import BigInteger, column, delete, select, update, values
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from abstract_customer import get_customer_json, get_lead_json
from amocrm import Customer, Lead, COMPANIES, CONTACTS, amocrm_get
from models import AbstractCustomer, db

FIELDS = ('amo_customers', 'amo_leads', 'amo_contacts', 'amo_companies')
BATCH_SIZE = 500


def entity_id(item):
    return item['general']['id']


def login(item):
    return item['general'].get('login')


class Owner:
    def __init__(self, row=None):
        self.id = row['id'] if row else None
        self.items = {field: {entity_id(x): x for x in (row[field] or [])}
                      if row else {} for field in FIELDS}
        self.methods = {json.dumps(x, sort_keys=True, ensure_ascii=False): x
                        for x in ((row['send_methods'] or []) if row else [])}
        self.seen = {field: set() for field in FIELDS}
        self.dirty = row is None

    def size(self):
        return sum(len(items) for items in self.items.values()) + len(self.methods)


class Sync:
    def __init__(self, rows):
        self.owners = set()
        self.deleted = set()
        self.ids = {field: defaultdict(set) for field in FIELDS[:2]}
        self.logins = {field: defaultdict(set) for field in FIELDS[:2]}
        # Counts avoid removing an owner from a login index while another entity
        # within that same owner still has the login.
        self.login_counts = {field: defaultdict(int) for field in FIELDS[:2]}
        for row in rows:
            owner = Owner(row)
            self.owners.add(owner)
            for field in FIELDS[:2]:
                for item in owner.items[field].values():
                    self.add_index(owner, field, item)

    def add_index(self, owner, field, item):
        self.ids[field][entity_id(item)].add(owner)
        key = login(item)
        if key:
            self.login_counts[field][owner, key] += 1
            self.logins[field][key].add(owner)

    def remove_index(self, owner, field, item):
        ident = entity_id(item)
        self.ids[field][ident].discard(owner)
        if not self.ids[field][ident]:
            del self.ids[field][ident]
        key = login(item)
        if key:
            counts = self.login_counts[field]
            counts[owner, key] -= 1
            if not counts[owner, key]:
                del counts[owner, key]
                self.logins[field][key].discard(owner)
                if not self.logins[field][key]:
                    del self.logins[field][key]

    def find_id(self, field, ident):
        matches = self.ids[field].get(ident, set())
        if len(matches) > 1:
            raise ValueError('Duplicate CRM id %r in %s' % (ident, field))
        return next(iter(matches), None)

    def find_login(self, field, key):
        matches = self.logins[field].get(key, set()) if key else set()
        return next(iter(matches)) if len(matches) == 1 else None

    def new_owner(self):
        owner = Owner()
        self.owners.add(owner)
        return owner

    def upsert(self, owner, field, item):
        ident = entity_id(item)
        old = owner.items[field].get(ident)
        if old is not None:
            self.remove_index(owner, field, old)
        owner.items[field][ident] = item
        self.add_index(owner, field, item)
        owner.seen[field].add(ident)
        owner.dirty = True

    def merge(self, target, source):
        # Move the smaller set of entity indexes. The surviving database ID is
        # always the lead's owner ID, independently of the chosen container.
        survivor_id = target.id
        if source.id is not None:
            self.deleted.add(source.id)
        if target.size() < source.size():
            large, small = source, target
        else:
            large, small = target, source
        for field in FIELDS:
            for ident, item in small.items[field].items():
                if field in self.ids:
                    self.remove_index(small, field, item)
                existing = large.items[field].get(ident)
                # If both contain the same entity, target's version wins.
                chosen = item if small is target else existing or item
                if existing is not None and field in self.ids and chosen is item:
                    self.remove_index(large, field, existing)
                if existing is None or chosen is item:
                    large.items[field][ident] = chosen
                    if field in self.ids:
                        self.add_index(large, field, chosen)
            large.seen[field].update(small.seen[field])
        large.methods.update(small.methods)
        large.id = survivor_id
        large.dirty = True
        self.owners.remove(small)
        return large

    def collect_customer(self, customer):
        key = customer.get_field('Логин')
        if not key:
            return
        owner = (self.find_id('amo_customers', customer.id)
                 or self.find_login('amo_leads', key) or self.new_owner())
        self.upsert(owner, 'amo_customers', get_customer_json(customer))
        self.mark_related(owner, customer)

    def collect_lead(self, lead):
        owner = self.find_id('amo_leads', lead.id)
        match = self.find_login('amo_customers', lead.get_field('Логин'))
        if owner and match and owner is not match:
            owner = self.merge(owner, match)
        owner = owner or match or self.new_owner()
        self.upsert(owner, 'amo_leads', get_lead_json(lead))
        self.mark_related(owner, lead)

    def mark_related(self, owner, entity):
        for field, attr in (('amo_contacts', 'contacts'), ('amo_companies', 'companies')):
            owner.seen[field].update(x['id'] for x in getattr(entity, attr, []))

    def cleanup(self):
        for owner in list(self.owners):
            for field in FIELDS:
                owner.items[field] = {ident: item for ident, item in owner.items[field].items()
                                      if ident in owner.seen[field]
                                      and 'удалить' not in str(item.get('general', {}).get('name') or item.get('name') or '').lower()}
            if not owner.items['amo_customers'] and not owner.items['amo_leads']:
                if owner.id is not None:
                    self.deleted.add(owner.id)
                self.owners.remove(owner)
            else:
                owner.dirty = True

    def persist(self, session):
        table = AbstractCustomer.__table__
        inserts, updates = [], []
        for owner in self.owners:
            if not owner.dirty:
                continue
            data = {field: list(owner.items[field].values()) for field in FIELDS}
            data['send_methods'] = list(owner.methods.values())
            if owner.id is None:
                inserts.append(data)
            else:
                updates.append(dict(id=owner.id, **data))
        deleted_ids = list(self.deleted)
        for offset in range(0, len(deleted_ids), BATCH_SIZE):
            ids = deleted_ids[offset:offset + BATCH_SIZE]
            session.execute(delete(table).where(table.c.id.in_(ids)))
        for offset in range(0, len(inserts), BATCH_SIZE):
            session.execute(table.insert().values(inserts[offset:offset + BATCH_SIZE]))
        for offset in range(0, len(updates), BATCH_SIZE):
            chunk = updates[offset:offset + BATCH_SIZE]
            fields = FIELDS + ('send_methods',)
            payload = values(column('id', BigInteger), *(column(f, JSONB) for f in fields), name='payload').data(
                [tuple(row[k] for k in ('id',) + fields) for row in chunk])
            stmt = update(table).where(table.c.id == payload.c.id).values(
                {**{f: getattr(payload.c, f) for f in fields}, 'updated_at': func.now()})
            session.execute(stmt)


def collect_leads_customers(is_full=True, crm_get=None):
    """Collect both feeds; exceptions roll back every write including cleanup.

    crm_get receives (entity_class, _with=...) and returns an iterator. A real
    CRM integration must supply a complete feed for a full synchronization.
    """
    crm_get = crm_get or amocrm_get
    session = db.session
    try:
        table = AbstractCustomer.__table__
        rows = session.execute(select(table)).mappings().all()
        sync = Sync(rows)
        del rows
        for customer in crm_get(Customer, _with=f'{COMPANIES},{CONTACTS}'):
            if is_full or customer.status_id != 111349:
                sync.collect_customer(customer)
        for lead in crm_get(Lead, _with=CONTACTS):
            if is_full or lead.status_id != 143:
                sync.collect_lead(lead)
        if is_full:
            sync.cleanup()
        sync.persist(session)
        session.commit()
    except Exception:
        session.rollback()
        raise
