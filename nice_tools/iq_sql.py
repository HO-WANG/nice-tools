"""Conservative lexical gate for a single IQ SELECT statement.

This checks statement shape, not database permissions or function side effects.
Use an account with the database permissions appropriate for the query.
"""

_MAX_SQL_BYTES = 1024 * 1024
_FORBIDDEN = frozenset('''
    INTO INSERT UPDATE DELETE MERGE CREATE ALTER DROP TRUNCATE GRANT REVOKE
    CALL EXEC EXECUTE OUTPUT UNLOAD LOAD COMMIT ROLLBACK SAVEPOINT SET BEGIN
    DECLARE CONNECT DISCONNECT ATTACH DETACH BACKUP RESTORE SHUTDOWN
    CHECKPOINT LOCK UNLOCK READTEXT WRITETEXT UPDATETEXT PRINT RAISERROR
'''.split())
_ERROR = 'SQL must contain one well-formed SELECT or WITH ... SELECT statement.'


def _tokens(sql):
    """Scan quotes and nested comments without exposing their contents."""
    result = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char.isspace():
            index += 1
        elif sql.startswith('--', index):
            index += 2
            while index < len(sql) and sql[index] not in '\r\n':
                index += 1
        elif sql.startswith('/*', index):
            index += 2
            depth = 1
            while index < len(sql) and depth:
                if sql.startswith('/*', index):
                    depth += 1
                    index += 2
                elif sql.startswith('*/', index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise ValueError(_ERROR)
        elif char in "'\"[":
            end = ']' if char == '[' else char
            kind = 'literal' if char == "'" else 'identifier'
            index += 1
            while index < len(sql):
                if sql[index] == end:
                    index += 1
                    if index < len(sql) and sql[index] == end:
                        index += 1
                    else:
                        break
                else:
                    index += 1
            else:
                raise ValueError(_ERROR)
            result.append((kind, ''))
        elif char.isalpha() or char in '_@#$':
            begin = index
            index += 1
            while index < len(sql) and (sql[index].isalnum() or sql[index] in '_@#$'):
                index += 1
            word = sql[begin:index].upper()
            if word in _FORBIDDEN:
                raise ValueError(_ERROR)
            result.append(('word', word))
        else:
            result.append(('symbol', char))
            index += 1
    return result


def _query(tokens, pairs, start, stop):
    def word(index, value):
        return index < stop and tokens[index] == ('word', value)

    if word(start, 'WITH'):
        index = start + 1
        if word(index, 'RECURSIVE'):
            index += 1
        while True:
            if index >= stop or tokens[index][0] not in {'word', 'identifier'}:
                raise ValueError(_ERROR)
            index += 1
            if index < stop and tokens[index] == ('symbol', '('):
                end = pairs[index]
                names = tokens[index + 1:end]
                if not names or any(token[0] not in {'word', 'identifier'} if i % 2 == 0
                                    else token != ('symbol', ',') for i, token in enumerate(names)):
                    raise ValueError(_ERROR)
                if len(names) % 2 == 0:
                    raise ValueError(_ERROR)
                index = end + 1
            if not word(index, 'AS') or index + 1 >= stop or tokens[index + 1] != ('symbol', '('):
                raise ValueError(_ERROR)
            opening = index + 1
            closing = pairs[opening]
            _query(tokens, pairs, opening + 1, closing)
            index = closing + 1
            if index < stop and tokens[index] == ('symbol', ','):
                index += 1
                continue
            start = index
            break
    if not word(start, 'SELECT'):
        raise ValueError(_ERROR)
    index = start
    previous = before_previous = None
    set_operators = {('word', name) for name in ('UNION', 'INTERSECT', 'EXCEPT')}
    while index < stop:
        token = tokens[index]
        if token == ('symbol', '('):
            end = pairs[index]
            if index + 1 < end and tokens[index + 1] in {('word', 'SELECT'), ('word', 'WITH')}:
                _query(tokens, pairs, index + 1, end)
            else:
                # Visit each nested query once, including those inside functions.
                nested = index + 1
                while nested < end:
                    if tokens[nested] == ('symbol', '(') and nested + 1 < end:
                        if tokens[nested + 1] in {('word', 'SELECT'), ('word', 'WITH')}:
                            _query(tokens, pairs, nested + 1, pairs[nested])
                            nested = pairs[nested] + 1
                            continue
                    if tokens[nested] == ('word', 'SELECT'):
                        raise ValueError(_ERROR)
                    nested += 1
            before_previous, previous = previous, ('symbol', ')')
            index = end + 1
            continue
        if token == ('word', 'SELECT'):
            after_union = previous in set_operators or (
                previous in {('word', 'ALL'), ('word', 'DISTINCT')} and before_previous in set_operators)
            if index != start and not after_union:
                raise ValueError(_ERROR)
            if index + 1 >= stop or tokens[index + 1] in {
                ('word', 'SELECT'), ('word', 'FROM'), ('word', 'UNION'), ('symbol', ')'),
            }:
                raise ValueError(_ERROR)
        before_previous, previous = previous, token
        index += 1
    if previous in set_operators or previous in {('word', 'ALL'), ('word', 'DISTINCT'), ('symbol', ',')}:
        raise ValueError(_ERROR)


def validate_select(sql: object) -> str:
    """Validate one SELECT; return the original SQL with outside space removed."""
    if not isinstance(sql, str) or not sql.strip() or len(sql) > _MAX_SQL_BYTES:
        raise ValueError(_ERROR)
    try:
        size = len(sql.encode('utf-8', errors='strict'))
    except UnicodeError:
        raise ValueError(_ERROR) from None
    if size > _MAX_SQL_BYTES or any(ord(char) < 32 and char not in '\t\r\n' for char in sql):
        raise ValueError(_ERROR)
    tokens = _tokens(sql)
    if tokens and tokens[-1] == ('symbol', ';'):
        tokens.pop()
    pairs, stack = {}, []
    for index, token in enumerate(tokens):
        if token == ('symbol', ';'):
            raise ValueError(_ERROR)
        if token == ('symbol', '('):
            stack.append(index)
            if len(stack) > 128:
                raise ValueError(_ERROR)
        elif token == ('symbol', ')'):
            if not stack:
                raise ValueError(_ERROR)
            pairs[stack.pop()] = index
    if stack:
        raise ValueError(_ERROR)
    _query(tokens, pairs, 0, len(tokens))
    return sql.strip()
