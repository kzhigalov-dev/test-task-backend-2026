import os

import pytest
from flask import Flask
from sqlalchemy import event, select

from amocrm import Customer, Lead
from main import collect_leads_customers
from models import AbstractCustomer, db


def item(id, login=None, name='name'):
    return {'general': {'id': id, 'login': login, 'name': name}, 'api_value': {}}


def customer(id, login, name='customer', contacts=None, companies=None, status=1):
    return Customer(id, name, None, None, None, status, None, None, None,
                    None, None, None, None, None, None, None, None,
                    {'Логин': {'value': login}},
                    {'contacts': contacts or [], 'companies': companies or []})


def lead(id, login=None, name='lead', contacts=None, status=1):
    return Lead(id=id, name=name, status_id=status,
                custom_fields_values={'Логин': {'value': login}},
                _embedded={'contacts': contacts or []})


def feed(customers=(), leads=()):
    return lambda cls, **kw: iter(customers if cls is Customer else leads)


@pytest.fixture
def app():
    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        pytest.skip('set TEST_DATABASE_URL to a dedicated PostgreSQL test database')
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI=url, TESTING=True)
    db.init_app(app)
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def rows():
    return db.session.execute(select(AbstractCustomer).order_by(AbstractCustomer.id)).scalars().all()


def test_new_entities_and_incremental_exclusions(app):
    collect_leads_customers(False, feed([customer(1, None), customer(2, 'x'), customer(3, 'z', status=111349)],
                                         [lead(10, 'x'), lead(11), lead(12, 'z', status=143)]))
    result = rows()
    assert len(result) == 2
    matched = next(row for row in result if row.amo_customers)
    lone = next(row for row in result if not row.amo_customers)
    assert matched.amo_customers[0]['general']['id'] == 2
    assert matched.amo_leads[0]['general']['id'] == 10
    assert lone.amo_leads[0]['general']['id'] == 11


def test_customer_id_precedence_and_ambiguous_login(app):
    db.session.add_all([
        AbstractCustomer(amo_customers=[item(1, 'old')]),
        AbstractCustomer(amo_leads=[item(10, 'same'), item(11, 'same')]),
        AbstractCustomer(amo_leads=[item(12, 'same')]),
    ])
    db.session.commit()
    collect_leads_customers(False, feed([customer(1, 'same'), customer(2, 'same')]))
    result = rows()
    assert len(result) == 4
    assert result[0].amo_customers[0]['general']['login'] == 'same'
    assert result[3].amo_customers[0]['general']['id'] == 2


def test_merge_then_cleanup_retains_seen_data_and_target_id(app):
    db.session.add_all([
        AbstractCustomer(amo_leads=[item(10, 'old')], amo_contacts=[item(100)], send_methods=[{'type': 'mail', 'value': 'a'}]),
        AbstractCustomer(amo_customers=[item(1, 'x')], amo_companies=[item(200)],
                         amo_contacts=[item(101)], send_methods=[{'type': 'sms', 'value': 'b'}]),
    ])
    db.session.commit()
    target_id = rows()[0].id
    collect_leads_customers(True, feed([customer(1, 'x', contacts=[{'id': 101}], companies=[{'id': 200}])],
                                        [lead(10, 'x', contacts=[{'id': 100}]), lead(11, 'x')]))
    result = rows()
    assert len(result) == 1 and result[0].id == target_id
    assert {x['general']['id'] for x in result[0].amo_leads} == {10, 11}
    assert {x['general']['id'] for x in result[0].amo_contacts} == {100, 101}
    assert result[0].amo_companies[0]['general']['id'] == 200
    assert len(result[0].send_methods) == 2


def test_full_cleanup_empty_and_delete_names(app):
    db.session.add(AbstractCustomer(amo_customers=[item(1, 'x')], amo_contacts=[item(2, name='УДАЛИТЬ')]))
    db.session.commit()
    collect_leads_customers(True, feed([customer(1, 'x', name='удалить', contacts=[{'id': 2}])]))
    assert rows() == []
    db.session.add(AbstractCustomer(amo_leads=[item(5)]))
    db.session.commit()
    collect_leads_customers(True, feed())
    assert rows() == []


def test_generator_failure_rolls_back(app):
    db.session.add(AbstractCustomer(amo_customers=[item(1, 'old')]))
    db.session.commit()
    def broken(cls, **kwargs):
        if cls is Customer:
            yield customer(1, 'new')
        else:
            yield lead(7, 'new')
            raise RuntimeError('CRM failed')
    with pytest.raises(RuntimeError):
        collect_leads_customers(True, broken)
    assert rows()[0].amo_customers[0]['general']['login'] == 'old'
    assert rows()[0].amo_leads is None


def test_write_failure_rolls_back(app):
    db.session.add(AbstractCustomer(amo_customers=[item(1, 'old')]))
    db.session.commit()
    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('UPDATE'):
            raise RuntimeError('write failed')
    event.listen(db.engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError):
            collect_leads_customers(True, feed([customer(1, 'new')], [lead(2)]))
    finally:
        event.remove(db.engine, 'before_cursor_execute', fail)
    assert len(rows()) == 1 and rows()[0].amo_customers[0]['general']['login'] == 'old'


def test_batch_statements_and_jsonb_lookup(app):
    statements = []
    def count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(db.engine, 'before_cursor_execute', count)
    try:
        collect_leads_customers(True, feed([customer(i, 'u%d' % i) for i in range(1, 1101)]))
    finally:
        event.remove(db.engine, 'before_cursor_execute', count)
    assert len(statements) == 4  # one select and three multi-row inserts
    assert db.session.query(AbstractCustomer).filter(AbstractCustomer.amo_customers.contains([{'general': {'login': 'u1'}}])).count() == 1


def test_repeated_merges_update_indexes_and_seen_sets(app):
    db.session.add_all([
        AbstractCustomer(amo_leads=[item(10, 'a')]),
        AbstractCustomer(amo_leads=[item(20, 'b')]),
        AbstractCustomer(amo_leads=[item(30, 'c')]),
        AbstractCustomer(amo_customers=[item(1, 'x'), item(2, 'x')]),
    ])
    db.session.commit()
    ids = [x.id for x in rows()]
    collect_leads_customers(True, feed([customer(1, 'x'), customer(2, 'x')],
                                      [lead(10, 'x'), lead(20, 'x'), lead(30, 'x')]))
    result = rows()
    assert len(result) == 1 and result[0].id == ids[2]
    assert {x['general']['id'] for x in result[0].amo_customers} == {1, 2}
    assert {x['general']['id'] for x in result[0].amo_leads} == {10, 20, 30}


def test_changed_login_no_longer_matches_old_login(app):
    db.session.add(AbstractCustomer(amo_customers=[item(1, 'old')]))
    db.session.commit()
    collect_leads_customers(False, feed([customer(1, 'new')], [lead(2, 'old'), lead(3, 'new')]))
    result = rows()
    assert len(result) == 2
    assert {x['general']['id'] for x in result[0].amo_leads} == {3}
    assert result[1].amo_leads[0]['general']['id'] == 2


def test_customer_fallback_multiple_leads_same_owner(app):
    db.session.add(AbstractCustomer(amo_leads=[item(10, 'x'), item(11, 'x')]))
    db.session.commit()
    existing = rows()[0].id
    collect_leads_customers(False, feed([customer(1, 'x')]))
    assert len(rows()) == 1 and rows()[0].id == existing
    assert rows()[0].amo_customers[0]['general']['id'] == 1


def test_cleanup_prunes_unseen_and_marked_children_of_live_owner(app):
    db.session.add(AbstractCustomer(
        amo_customers=[item(1, 'x'), item(2, 'old'), item(3, 'y')],
        amo_leads=[item(10, 'x'), item(11, 'old'), item(12, 'y')],
        amo_contacts=[item(100), item(101), item(102, name='УДАЛИТЬ')],
        amo_companies=[item(200), item(201), item(202, name='удалить')]))
    db.session.commit()
    collect_leads_customers(True, feed(
        [customer(1, 'x', contacts=[{'id': 100}, {'id': 102}], companies=[{'id': 200}, {'id': 202}]),
         customer(3, 'y', name='УДАЛИТЬ')],
        [lead(10, 'x'), lead(12, 'y', name='удалить')]))
    result = rows()[0]
    assert [x['general']['id'] for x in result.amo_customers] == [1]
    assert [x['general']['id'] for x in result.amo_leads] == [10]
    assert [x['general']['id'] for x in result.amo_contacts] == [100]
    assert [x['general']['id'] for x in result.amo_companies] == [200]


def test_updates_and_deletes_are_batched(app):
    db.session.add_all([AbstractCustomer(amo_customers=[item(i, 'u%d' % i)]) for i in range(1, 1101)])
    db.session.commit()
    statements = []
    def count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(db.engine, 'before_cursor_execute', count)
    try:
        collect_leads_customers(True, feed([customer(i, 'u%d' % i) for i in range(1, 601)]))
    finally:
        event.remove(db.engine, 'before_cursor_execute', count)
    assert len(statements) == 4  # select, one delete, two updates
    assert len(rows()) == 600
