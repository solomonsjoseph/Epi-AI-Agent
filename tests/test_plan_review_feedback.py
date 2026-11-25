from __future__ import annotations

import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError

from api.schemas import ResumeInterruptRequest
from epi_agent.artifacts import DatasetPlan
from epi_agent.db_rag.reviews import RequestDatasetPlanReviewTool
from epi_agent.protocol import ToolExecutionError
from utils.review_interrupts import InvalidInterruptDecisionError, validate_resume_decision
from test_review_output_selection import runtime


def review_graph(runtime):
    context, _, _, fields = runtime
    plan = DatasetPlan(study_id='study', goal='Select age and sex', concepts=[{'concept_id': field['column'], 'label': field['column'], 'fields': [field]} for field in fields[1:]], required_fields=[fields[0]])
    draft = context.artifact_store.save_dataset_plan(plan)

    def review(state):
        result = RequestDatasetPlanReviewTool().invoke({'plan_id': draft.id, 'version': draft.version}, context)
        return {'message': result.message, 'decision': result.conversation_review_decision}

    builder = StateGraph(dict)
    builder.add_node('review', review)
    builder.add_edge(START, 'review')
    builder.add_edge('review', END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {'configurable': {'thread_id': 'feedback'}}
    assert graph.invoke({}, config)['__interrupt__']
    return graph, config, context, draft


def interrupt():
    return {'type': 'dataset_plan_review', 'view': {'concept_groups': [{'concept_id': 'age'}, {'concept_id': 'sex'}]}}


def test_scope_is_accepted_only_for_revision_and_normalized_against_concepts():
    request = ResumeInterruptRequest(action='revise', feedback="don't choose this", review_concept_id=' sex ', selected_column_keys=['source::participants::age'])
    payload = request.model_dump(exclude_none=True)
    assert payload['review_concept_id'] == 'sex'
    assert validate_resume_decision(interrupt(), payload) == payload
    with pytest.raises(ValidationError):
        ResumeInterruptRequest(action='approve', review_concept_id='sex')
    with pytest.raises(InvalidInterruptDecisionError):
        validate_resume_decision(interrupt(), {**payload, 'review_concept_id': 'missing'})
    with pytest.raises(InvalidInterruptDecisionError):
        validate_resume_decision({'type': 'dataset_review'}, payload)


@pytest.mark.parametrize('scope', ['sex', None])
def test_revision_exposes_selection_difference_and_optional_scope(runtime, scope):
    graph, config, context, _ = review_graph(runtime)
    payload = {'action': 'revise', 'feedback': "don't choose this", 'selected_column_keys': ['source::participants::age']}
    if scope:
        payload['review_concept_id'] = scope
    result = graph.invoke(Command(resume=payload), config)
    revised = max(context.artifact_store.list_artifacts(kind='dataset_plan'), key=lambda item: item.version)
    feedback = revised.provenance['review_context']
    assert feedback['concept_id'] == scope
    assert feedback['selected_column_keys'] == ['source::participants::age']
    assert feedback['excluded_column_keys'] == ['source::participants::sex']
    assert json.dumps(feedback, sort_keys=True) in result['message']
    assert result['decision']['text'] == "don't choose this"


def test_direct_tool_rejects_unknown_scope_before_saving_revision(runtime):
    graph, config, context, _ = review_graph(runtime)
    before = len(context.artifact_store.list_artifacts(kind='dataset_plan'))
    with pytest.raises(ToolExecutionError) as error:
        graph.invoke(Command(resume={'action': 'revise', 'feedback': "don't choose this", 'review_concept_id': 'missing'}), config)
    assert error.value.code == 'REVIEW_DECISION_INVALID'
    assert len(context.artifact_store.list_artifacts(kind='dataset_plan')) == before


def test_empty_approval_is_rejected_before_interrupt_resume(runtime):
    graph, config, _, _ = review_graph(runtime)
    before = graph.get_state(config).interrupts
    with pytest.raises(InvalidInterruptDecisionError, match='Select at least one variable'):
        validate_resume_decision(interrupt(), {'action': 'approve', 'selected_column_keys': []})
    assert graph.get_state(config).interrupts == before


def test_direct_tool_rejects_empty_approval_without_artifact_creation(runtime):
    graph, config, context, _ = review_graph(runtime)
    before = len(context.artifact_store.list_artifacts(kind='dataset_plan'))
    with pytest.raises(ToolExecutionError) as error:
        graph.invoke(Command(resume={'action': 'approve', 'selected_column_keys': []}), config)
    assert error.value.code == 'REVIEW_SELECTION_REQUIRED'
    assert len(context.artifact_store.list_artifacts(kind='dataset_plan')) == before
    assert not context.artifact_store.snapshot().get('datasets')


def test_empty_revision_delivers_feedback_and_keeps_reference_only_draft(runtime):
    graph, config, context, original = review_graph(runtime)
    prior_content = context.artifact_store.require(original).content
    result = graph.invoke(Command(resume={'action': 'revise', 'selected_column_keys': [], 'feedback': 'Use marital status instead.'}), config)
    revised = max(context.artifact_store.list_artifacts(kind='dataset_plan'), key=lambda item: item.version)
    assert revised.status == 'draft'
    assert revised.content == prior_content
    review_context = revised.provenance['review_context']
    assert review_context['selection_empty'] is True
    assert review_context['selected_column_keys'] == []
    assert review_context['excluded_column_keys'] == ['source::participants::age', 'source::participants::sex']
    assert result['decision']['text'] == 'Use marital status instead.'
    assert 'reference only' in result['message']
    assert not context.artifact_store.snapshot().get('datasets')
    with pytest.raises(ToolExecutionError) as error:
        RequestDatasetPlanReviewTool().invoke({'plan_id': revised.id, 'version': revised.version}, context)
    assert error.value.code == 'PLAN_REVISION_REQUIRED'
