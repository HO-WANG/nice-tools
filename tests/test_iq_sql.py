import importlib

import pytest


def test_validator_module_is_available():
    try:
        importlib.import_module('nice_tools.iq_sql')
    except ModuleNotFoundError:
        pytest.fail('IQ SELECT validation is not implemented.')


@pytest.fixture
def iq_sql():
    try:
        return importlib.import_module('nice_tools.iq_sql')
    except ModuleNotFoundError:
        pytest.fail('IQ SELECT validation is not implemented.')


@pytest.mark.parametrize('sql', [
    'SELECT 1', '  select value from owner.table  ',
    "SELECT 'delete; INTO ''quoted''', \"EXEC\", [UNLOAD]]name] FROM t; -- done",
    '/* SELECT /* nested */ comments */ SELECT 1 /* tail */;',
    'WITH c AS (SELECT 1 AS x) SELECT x FROM c',
    'WITH c(x) AS (SELECT 1), d AS (SELECT x FROM c) SELECT * FROM d;',
    'WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT * FROM c',
    'SELECT (SELECT count(*) FROM t) AS n FROM u UNION ALL SELECT 1 FROM v',
    'SELECT "with", [into], \'CALL\' FROM t -- comment\n; /* end */',
    'SELECT CASE WHEN x=1 THEN 2 ELSE 3 END FROM t',
    'SELECT coalesce((SELECT 1), 0) FROM t',
])
def test_accepts_one_select_and_preserves_sql(iq_sql, sql):
    assert iq_sql.validate_select(sql) == sql.strip()


@pytest.mark.parametrize('sql', [
    None, 1, [], '', ' -- only comment', ';', 'SELECT 1; SELECT 2',
    'SELECT 1;;', 'SELECT 1 SELECT 2', 'SELECT (SELECT 1 SELECT 2)',
    'SELECT 1 ALL SELECT 2', 'SELECT 1 DISTINCT SELECT 2',
    'SELECT (x SELECT 2)', 'SELECT 1 UNION',
    'SELECT value INTO copied FROM t', 'WITH c AS (SELECT 1 INTO x) SELECT * FROM c',
    'WITH c AS (SELECT 1) DELETE FROM t', 'WITH c AS (DELETE FROM t) SELECT * FROM c',
    'WITH c AS (SELECT 1)', 'WITH SELECT 1', 'WITH c (SELECT 1) SELECT * FROM c',
    'INSERT INTO t SELECT 1', 'UPDATE t SET x=1', 'DELETE FROM t',
    'CALL proc()', 'EXEC proc', 'OUTPUT TO file', 'UNLOAD SELECT 1 TO file',
    'SELECT 1; /* harmless */ CALL p()', 'SELECT 1 OUTPUT TO file',
    'SELECT 1 UNION DELETE FROM t', 'SELECT 1 CREATE TABLE t(x int)',
    'SELECT 1 -- comment\n DROP TABLE t', 'SELECT 1 /* unterminated',
    "SELECT 'unterminated", 'SELECT "unterminated', 'SELECT [unterminated',
    'SELECT (1', 'SELECT 1)', 'SELECT \x00secret', 'SELECT ' + 'x' * 1048576,
    "SELECT '" + '한' * 400000 + "'",
], ids=lambda sql: str(type(sql).__name__) if not isinstance(sql, str) else sql[:70])
def test_rejects_nonselect_malformed_or_multiple_without_sql_values(iq_sql, sql):
    with pytest.raises(ValueError) as error:
        iq_sql.validate_select(sql)
    assert 'secret' not in str(error.value)
    assert 'unterminated' not in str(error.value)


@pytest.mark.parametrize('word', [
    'INSERT', 'UPDATE', 'DELETE', 'MERGE', 'CREATE', 'ALTER', 'DROP', 'TRUNCATE',
    'GRANT', 'REVOKE', 'CALL', 'EXEC', 'EXECUTE', 'OUTPUT', 'UNLOAD', 'LOAD',
    'COMMIT', 'ROLLBACK', 'SET', 'BEGIN', 'DECLARE', 'CONNECT', 'DISCONNECT',
])
def test_forbidden_commands_outside_quotes_are_rejected(iq_sql, word):
    with pytest.raises(ValueError):
        iq_sql.validate_select('SELECT 1 ' + word + ' private_value')
