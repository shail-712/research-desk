"""Guards on template.yaml that `sam validate --lint` doesn't check: the never-list (SPEC §3),
cost rules, Retain on the index bucket, log retention, scoped permissions.

Adapted from v1 tests/unit/test_template.py (73b6fda): the dashboard, tracing and SES checks are
replaced by the D13 cuts (no dashboard, no X-Ray, no digest)."""

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_TEXT = (ROOT / "template.yaml").read_text(encoding="utf-8")


class _CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {f"!{suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"!{suffix}": loader.construct_sequence(node, deep=True)}
    return {f"!{suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag)


@pytest.fixture(scope="module")
def template():
    return yaml.load(TEMPLATE_TEXT, Loader=_CfnLoader)


def _resources(template, type_):
    return {k: v for k, v in template["Resources"].items() if v["Type"] == type_}


def _functions(template):
    return _resources(template, "AWS::Serverless::Function")


def _statements(fn) -> list[dict]:
    out = []
    for policy in fn["Properties"].get("Policies", []):
        if isinstance(policy, dict) and isinstance(policy.get("Statement"), list):
            out += policy["Statement"]
    return out


# ── never-list (SPEC §3, AGENT.md) and D13 cuts ──

NEVER_TYPES = ("AWS::EC2::NatGateway", "AWS::EC2::VPC", "AWS::SecretsManager::", "AWS::CloudFront::",
               "AWS::OpenSearchService::", "AWS::Elasticsearch::", "AWS::RDS::", "AWS::Bedrock",
               "AWS::CloudWatch::Dashboard")


def test_nothing_from_the_never_list(template):
    types = {v["Type"] for v in template["Resources"].values()}
    assert not [t for t in types if t.startswith(NEVER_TYPES)]
    for name, fn in _functions(template).items():
        props = fn["Properties"]
        assert "VpcConfig" not in props, f"{name}: Lambda in a VPC needs a NAT for Gemini/Yahoo"
        assert "ProvisionedConcurrencyConfig" not in props and "AutoPublishAlias" not in props
    lowered = TEMPLATE_TEXT.lower()
    for word in ("bedrock", "secretsmanager", "pay_per_request", "provisionedconcurrency", "natgateway"):
        assert word not in lowered, word


def test_no_xray_tracing(template):
    """D13: X-Ray is cut (SAM would add the tracing permissions and the per-trace cost)."""
    assert "Tracing" not in template["Globals"]["Function"]
    assert all("Tracing" not in fn["Properties"] for fn in _functions(template).values())
    assert "Tracing" not in template["Resources"]["HttpApi"]["Properties"]


# ── cost ──

def test_dynamodb_stays_on_the_free_tier_and_matches_the_code(template):
    from core.store_dynamo import TABLE_DEFINITION

    table = template["Resources"]["Table"]["Properties"]
    assert table["BillingMode"] == "PROVISIONED"
    assert table["ProvisionedThroughput"] == {"ReadCapacityUnits": 2, "WriteCapacityUnits": 2}
    assert table["KeySchema"] == TABLE_DEFINITION["KeySchema"]
    assert table["AttributeDefinitions"] == TABLE_DEFINITION["AttributeDefinitions"]
    assert table["TimeToLiveSpecification"] == {"AttributeName": "expires_at", "Enabled": True}
    assert table["StreamSpecification"]["StreamViewType"] == TABLE_DEFINITION["StreamSpecification"]["StreamViewType"]


def test_own_ecr_repository_keeps_two_images(template):
    repos = _resources(template, "AWS::ECR::Repository")
    assert len(repos) == 1
    policy = json.loads(next(iter(repos.values()))["Properties"]["LifecyclePolicy"]["LifecyclePolicyText"])
    selection = policy["rules"][0]["selection"]
    assert selection == {"tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": 2}
    assert policy["rules"][0]["action"] == {"type": "expire"}


def test_every_function_has_a_14_day_log_group(template):
    log_groups = _resources(template, "AWS::Logs::LogGroup").values()
    named = {json.dumps(g["Properties"]["LogGroupName"]) for g in log_groups}
    for name in _functions(template):
        assert any(f"${{{name}}}" in n for n in named), f"{name} has no log group with retention"
    assert all(g["Properties"]["RetentionInDays"] == 14 for g in log_groups)


def test_api_is_throttled_and_there_is_a_budget(template):
    routes = template["Resources"]["HttpApi"]["Properties"]["DefaultRouteSettings"]
    assert routes["ThrottlingBurstLimit"] <= 10 and routes["ThrottlingRateLimit"] <= 5
    budget = _resources(template, "AWS::Budgets::Budget")
    assert len(budget) == 1
    assert template["Parameters"]["BudgetLimitUsd"]["Default"] == 1


def test_timeouts_fit_api_gateway_and_the_deep_budget(template):
    from core.agents import deep

    fns = _functions(template)
    assert fns["ApiFunction"]["Properties"]["Timeout"] < 30            # HTTP API integration limit
    assert fns["AskWorkerFunction"]["Properties"]["Timeout"] > deep.DEEP_BUDGET_SECONDS + 15
    assert fns["AskWorkerFunction"]["Properties"]["EventInvokeConfig"]["MaximumRetryAttempts"] == 0


# ── data safety and security ──

def test_index_bucket_is_retained_private_and_encrypted(template):
    bucket = template["Resources"]["DataBucket"]
    assert bucket["DeletionPolicy"] == "Retain" and bucket["UpdateReplacePolicy"] == "Retain"
    block = bucket["Properties"]["PublicAccessBlockConfiguration"]
    assert block == {k: True for k in ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls",
                                       "RestrictPublicBuckets")}
    sse = bucket["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
    assert sse["ServerSideEncryptionByDefault"]["SSEAlgorithm"] in ("AES256", "aws:kms")


def test_ssm_read_is_scoped_to_the_secrets_path(template):
    for name, fn in _functions(template).items():
        ssm = [s for s in _statements(fn) if "ssm:GetParametersByPath" in s.get("Action", [])]
        assert ssm, f"{name} can't read its secrets"
        resources = json.dumps(ssm[0]["Resource"])
        assert "${SsmPath}" in resources and "parameter/*" not in resources
        kms = [s for s in _statements(fn) if s.get("Action") == ["kms:Decrypt"]]
        assert kms and "kms:ViaService" in kms[0]["Condition"]["StringEquals"]


def test_only_the_worker_reads_the_index_and_it_cannot_write(template):
    fns = _functions(template)
    s3 = [s for s in _statements(fns["AskWorkerFunction"]) if any(a.startswith("s3:") for a in s["Action"])]
    assert len(s3) == 1 and s3[0]["Action"] == ["s3:GetObject"]
    assert "/vectors/*" in json.dumps(s3[0]["Resource"])
    api_policies = json.dumps(fns["ApiFunction"]["Properties"]["Policies"])
    assert "s3" not in api_policies.lower()


def test_lambda_writes_go_to_tmp(template):
    env = template["Globals"]["Function"]["Environment"]["Variables"]
    assert env["CACHE_DIR"].startswith("/tmp/")
    assert env["EMBED_CACHE_PATH"].startswith("/tmp/")
    assert env["STORE_BACKEND"] == "dynamodb" and env["VECTOR_BACKEND"] == "s3"
    assert env["REQUIRE_ACCESS_CODE"] == "true"
