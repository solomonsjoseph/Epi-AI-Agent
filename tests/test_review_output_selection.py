from __future__ import annotations

import duckdb
import pandas as pd
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from db_rag.catalog import SchemaCatalog
from db_rag.catalog_relationships import CatalogRelationshipSpec
from db_rag.service.sql_service import SqlValidationError, validate_extraction_sql
from db_rag.study import DuckDbStudyDataSource
from epi_agent.artifacts import DatasetPlan, StateArtifactStore
from epi_agent.db_rag.reviews import RequestDatasetPlanReviewTool, _selected_plan
from epi_agent.db_rag.tools import build_db_rag_tool_registry
from epi_agent.protocol import ToolContext, ToolExecutionError
from epi_agent.studies import StudyBundle, StudyRegistry
from utils.user_storage import UserStorageLayout


@pytest.fixture
def runtime(tmp_path):
    database = tmp_path / 'study.duckdb'
    with duckdb.connect(str(database)) as connection:
        connection.execute('CREATE TABLE participants (id INTEGER, age INTEGER, sex INTEGER)')
        connection.execute('INSERT INTO participants VALUES (1, 30, 1), (2, 40, 2)')
    fields = [{'source': 'source', 'table': 'participants', 'column': name} for name in ['id', 'age', 'sex']]
    catalog = SchemaCatalog({'columns': [{**field, 'description': field['column']} for field in fields]}, default_source_id='source')
    study = StudyBundle(study_id='study', label='Study', knowledge=None, catalog=catalog, data_sources={'source': DuckDbStudyDataSource(database, CatalogRelationshipSpec(table_keys={'participants': {'participant_key': 'id'}}))}, source_id='source')
    plan = DatasetPlan(study_id='study', goal='Select age and sex', concepts=[{'concept_id': field['column'], 'label': field['column'], 'fields': [field]} for field in fields[1:]], required_fields=[fields[0]], operations=[{'name': 'select_base', 'source': 'source', 'left_table': 'participants', 'field_refs': fields}])
    frozen = _selected_plan(plan, ['source::participants::age'])
    store = StateArtifactStore()
    reference = store.save_dataset_plan(frozen, status='approved')
    storage = UserStorageLayout(tmp_path / 'storage').thread('test-user', 'deselection')
    context = ToolContext(studies=StudyRegistry([study]), artifact_store=store, thread_id=storage.thread_id, thread_storage=storage, policy=None)
    return context, reference, database, fields


@pytest.mark.parametrize('corrected_sql', [
    'SELECT id, age FROM participants WHERE sex = 1',
    'SELECT id, AVG(age) FILTER (WHERE sex=1) AS age FROM participants GROUP BY id',
    'SELECT id, FIRST(age ORDER BY sex DESC NULLS LAST) AS age FROM participants GROUP BY id',
    'SELECT id, ARG_MAX(age, sex) AS age FROM participants GROUP BY id',
])
def test_unchecked_output_is_rejected_before_persistence_then_same_plan_repairs(runtime, corrected_sql):
    context, reference, _, _ = runtime
    registry = build_db_rag_tool_registry()
    before = context.artifact_store.require(reference).model_dump()
    arguments = {'plan_id': reference.id, 'plan_version': reference.version}
    with pytest.raises(ToolExecutionError) as captured:
        registry.invoke('dbrag-validate_and_extract', {**arguments, 'sql': 'SELECT id, age, sex AS sex_code FROM participants'}, context=context)
    assert captured.value.code == 'SQL_REPAIR_REQUIRED'
    assert any(issue['code'] == 'SQL_UNAPPROVED_OUTPUT' for issue in captured.value.details['issues'])
    assert [field['column'] for field in captured.value.details['allowed_output_columns']] == ['id', 'age']
    assert not context.artifact_store.list_artifacts(kind='validated_sql')
    assert not context.artifact_store.snapshot().get('datasets')
    result = registry.invoke('dbrag-validate_and_extract', {**arguments, 'sql': corrected_sql}, context=context)
    dataset = context.artifact_store.require(result.artifacts[0])
    assert pd.read_parquet(dataset.content['path']).columns.tolist() == ['id', 'age']
    assert context.artifact_store.require(reference).model_dump() == before


def test_real_approval_returns_authoritative_output_summary(runtime):
    context, _, _, fields = runtime
    draft = context.artifact_store.save_dataset_plan(DatasetPlan(study_id='study', goal='Select age and sex', concepts=[{'concept_id': field['column'], 'label': field['column'], 'fields': [field]} for field in fields[1:]], required_fields=[fields[0]]))

    def review(state):
        result = RequestDatasetPlanReviewTool().invoke({'plan_id': draft.id, 'version': draft.version}, context)
        return {'message': result.message}

    builder = StateGraph(dict)
    builder.add_node('review', review)
    builder.add_edge(START, 'review')
    builder.add_edge('review', END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {'configurable': {'thread_id': 'approval-summary'}}
    assert graph.invoke({}, config)['__interrupt__']
    result = graph.invoke(Command(resume={'action': 'approve', 'selected_column_keys': ['source::participants::age']}), config)
    assert 'source::participants::age' in result['message']
    assert 'source::participants::id' in result['message']
    assert 'source::participants::sex' not in result['message'].split('Excluded field keys:')[0]
    assert 'Excluded field keys: ["source::participants::sex"]' in result['message']
    assert 'do not restore unchecked variables' in result['message']


@pytest.mark.parametrize('sql', [
    'SELECT id, age, sex FROM participants',
    'WITH chosen AS (SELECT id, age, sex AS hidden FROM participants) SELECT chosen.id, chosen.age, chosen.hidden FROM chosen',
    'SELECT id, age FROM participants UNION ALL SELECT id, sex AS age FROM participants',
    'SELECT id FROM participants',
    'SELECT id, age FROM participants UNION ALL BY NAME SELECT id, age, sex FROM participants',
    'SELECT id, age FROM participants UNION ALL SELECT id, id FROM participants',
    'SELECT id, id AS age FROM participants EXCEPT SELECT id, age FROM participants',
    'SELECT id, age, age AS another_age FROM participants',
    'SELECT id, ARG_MAX(sex, age) AS sex FROM participants GROUP BY id',
])
def test_exact_outputs_include_cte_and_union_lineage(runtime, sql):
    _, _, database, fields = runtime
    with pytest.raises(SqlValidationError) as captured:
        validate_extraction_sql(sql=sql, database_path=database, approved_tables=['participants'], approved_columns=fields, approved_output_columns=fields[:2])
    assert any(issue['stage'] == 'approved_output' for issue in captured.value.issues)


@pytest.mark.parametrize('sql', [
    'SELECT id, age FROM participants WHERE sex = 1',
    'WITH chosen AS (SELECT id, age, sex FROM participants) SELECT id, age FROM chosen WHERE sex = 1',
    'SELECT id, age FROM participants UNION ALL SELECT id, age FROM participants',
    'SELECT id, age FROM participants EXCEPT SELECT id, sex FROM participants',
    'WITH c AS (SELECT id, age FROM participants EXCEPT SELECT id, sex FROM participants) SELECT c.id, c.age FROM c',
    'SELECT id, age FROM participants UNION ALL (SELECT id, age FROM participants EXCEPT SELECT id, sex FROM participants)',
    'SELECT id, AVG(age) OVER (PARTITION BY id) AS age FROM participants',
    'SELECT id, FIRST_VALUE(age) OVER(PARTITION BY id ORDER BY sex DESC NULLS LAST) AS age FROM participants',
])
def test_support_fields_are_allowed_without_becoming_outputs(runtime, sql):
    _, _, database, fields = runtime
    assert validate_extraction_sql(sql=sql, database_path=database, approved_tables=['participants'], approved_columns=fields, approved_output_columns=fields[:2]).sql == sql
