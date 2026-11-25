"""Validate output permissions separately from SQL support references."""
from __future__ import annotations

from typing import Any


def approved_output_issues(
    expression: Any,
    *,
    reference_columns: list[dict[str, Any]],
    output_columns: list[dict[str, Any]],
) -> list[dict[str, str]]:
    from sqlglot import exp
    from sqlglot.lineage import lineage

    issues: list[dict[str, str]] = []

    def add(code: str, message: str) -> None:
        issues.append({'stage': 'approved_output', 'code': code, 'message': message})

    # BY NAME can add outputs that are absent from the left-hand SELECT,
    # whereas sqlglot's positional output lineage exposes only that SELECT.
    if any(node.args.get('by_name') for node in expression.find_all(exp.SetOperation)):
        add('SQL_OUTPUT_BY_NAME_UNSUPPORTED', 'Use positional set operations with the same explicit approved outputs in every branch; BY NAME may add unchecked outputs.')
        return issues

    def value_expression(node: Any) -> Any:
        # Lineage normally includes support columns from aggregate FILTER,
        # window partitions, and ordering. Inspect returned values on a copy;
        # permission checks and execution still use the complete original SQL.
        if isinstance(node, (exp.Window, exp.Filter, exp.Order, exp.ArgMax, exp.ArgMin)):
            return node.this
        if isinstance(node, (exp.Except, exp.Intersect)):
            left = node.this.copy()
            if node.args.get('with_'):
                inherited = node.args['with_'].copy()
                own = left.args.get('with_')
                if own:
                    inherited.set('expressions', [*inherited.expressions, *own.expressions])
                left.set('with_', inherited)
            return left
        return node

    # transform does not revisit replacement nodes: repeat until nested
    # membership operations and support wrappers have also been removed.
    values = expression.copy()
    while any(isinstance(node, (exp.Window, exp.Filter, exp.Order, exp.ArgMax, exp.ArgMin, exp.Except, exp.Intersect)) for node in values.walk()):
        values = values.transform(value_expression)

    def output_fields(node: Any) -> set[tuple[str, str]]:
        if node.downstream:
            # EXCEPT and INTERSECT use the right relation for membership only.
            downstream = node.downstream[:1] if isinstance(node.source, (exp.Except, exp.Intersect)) else node.downstream
            return set().union(*(output_fields(child) for child in downstream))
        if not isinstance(node.expression, exp.Table):
            raise ValueError('Output does not resolve to a physical field.')
        return {(node.expression.name.casefold(), exp.to_column(node.name).name.casefold())}

    approved = {(str(field['table']).casefold(), str(field['column']).casefold()) for field in output_columns}
    schema: dict[str, dict[str, str]] = {}
    for field in reference_columns:
        schema.setdefault(str(field['table']).casefold(), {})[str(field['column']).casefold()] = 'UNKNOWN'
    names = [projection.alias_or_name for projection in values.selects]
    if len(names) != len({name.casefold() for name in names}):
        add('SQL_OUTPUT_ALIAS_AMBIGUOUS', 'Give every approved output a unique SQL alias.')
        return issues
    observed: set[tuple[str, str]] = set()
    for name in names:
        try:
            root = lineage(name, values, schema=schema, dialect='duckdb')
            fields = output_fields(root)
            if not fields:
                raise ValueError('Output does not resolve to a physical field.')
        except Exception as error:
            add('SQL_OUTPUT_LINEAGE_UNRESOLVED', f'Output {name!r} cannot be matched to the approved fields: {error}')
            continue
        if len(fields) != 1:
            add('SQL_OUTPUT_SOURCE_MISMATCH', f'Output {name!r} must represent the same single approved source field in every output branch.')
        if fields & observed:
            add('SQL_DUPLICATE_OUTPUT', f'Output {name!r} repeats a source field already projected. Include each approved field once.')
        observed.update(fields)
        for table, column in sorted(fields - approved):
            add('SQL_UNAPPROVED_OUTPUT', f'Output {name!r} includes {table}.{column}, which is not an approved output. Support fields may be used for joins or filters but must not be projected.')
    for table, column in sorted(approved - observed):
        add('SQL_APPROVED_OUTPUT_MISSING', f'Approved output {table}.{column} is missing from the SQL result.')
    return issues
