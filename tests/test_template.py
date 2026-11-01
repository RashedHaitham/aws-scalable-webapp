"""Architecture invariants for cloudformation/webapp.yaml.

cfn-lint proves the template is valid CloudFormation; these tests prove it is the
architecture we intend (private compute, tiered security groups, Multi-AZ, ...).
"""
import ast
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from cfnlint.decode import decode

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "cloudformation" / "webapp.yaml"
PUBLIC, APP, DB = ["PublicSubnetA", "PublicSubnetB"], ["AppSubnetA", "AppSubnetB"], ["DbSubnetA", "DbSubnetB"]


def ref(name):
    return {"Ref": name}


def getatt(resource, attribute):
    return {"Fn::GetAtt": [resource, attribute]}


def of_type(template, resource_type):
    return {n: r for n, r in template["Resources"].items() if r["Type"] == resource_type}


def props(template, name):
    return template["Resources"][name].get("Properties", {})


def render_sub(node):
    """Resolve an Fn::Sub with placeholder values so embedded payloads can be inspected."""
    text = node["Fn::Sub"]
    text = text[0] if isinstance(text, list) else text
    text = re.sub(r"\$\{(?!!)([^}]+)\}", lambda m: "SUB_" + re.sub(r"\W", "_", m.group(1)), text)
    return text.replace("${!", "${")


def heredoc(script, path):
    match = re.search(r"cat > %s <<'EOF'\n(.*?)\nEOF\n" % re.escape(path), script, re.S)
    assert match, "no heredoc for " + path
    return match.group(1) + "\n"


@pytest.fixture(scope="session")
def template():
    tpl, errors = decode(str(TEMPLATE_PATH))
    assert not errors, errors
    return tpl


@pytest.fixture(scope="session")
def user_data(template):
    return render_sub(props(template, "LaunchTemplate")["LaunchTemplateData"]["UserData"]["Fn::Base64"])


def route_table_of(template, subnet):
    for assoc in of_type(template, "AWS::EC2::SubnetRouteTableAssociation").values():
        if assoc["Properties"]["SubnetId"] == ref(subnet):
            return assoc["Properties"]["RouteTableId"]["Ref"]
    raise AssertionError(subnet + " has no route table")


def default_route(template, route_table):
    for route in of_type(template, "AWS::EC2::Route").values():
        p = route["Properties"]
        if p["RouteTableId"] == ref(route_table) and p.get("DestinationCidrBlock") == "0.0.0.0/0":
            return p
    return None


def sg_rules(template, group, direction):
    """Inline plus standalone rules for a security group."""
    inline_key = "SecurityGroupIngress" if direction == "ingress" else "SecurityGroupEgress"
    standalone = "AWS::EC2::SecurityGroupIngress" if direction == "ingress" else "AWS::EC2::SecurityGroupEgress"
    rules = list(props(template, group).get(inline_key, []))
    rules += [r["Properties"] for r in of_type(template, standalone).values() if r["Properties"]["GroupId"] == ref(group)]
    return rules


# ---------------------------------------------------------------- deployability
def test_template_fits_direct_deploy_body_limit():
    # CreateStack/UpdateStack accept at most 51,200 bytes inline; larger needs an S3 upload.
    assert TEMPLATE_PATH.stat().st_size <= 51200


# ---------------------------------------------------------------- VPC
def test_each_tier_spans_two_availability_zones(template):
    for tier in (PUBLIC, APP, DB):
        azs = [props(template, s)["AvailabilityZone"]["Fn::Select"][0] for s in tier]
        assert sorted(azs) == [0, 1], tier


def test_no_subnet_auto_assigns_public_ips(template):
    for name, subnet in of_type(template, "AWS::EC2::Subnet").items():
        assert subnet["Properties"].get("MapPublicIpOnLaunch") is not True, name


def test_public_subnets_route_to_internet_gateway(template):
    for subnet in PUBLIC:
        assert default_route(template, route_table_of(template, subnet))["GatewayId"] == ref("InternetGateway")


def test_app_subnets_egress_through_nat_in_their_own_az(template):
    route_a = default_route(template, route_table_of(template, "AppSubnetA"))
    route_b = default_route(template, route_table_of(template, "AppSubnetB"))
    assert route_a["NatGatewayId"] == ref("NatGatewayA")
    assert route_b["NatGatewayId"] == {"Fn::If": ["NatPerAZ", ref("NatGatewayB"), ref("NatGatewayA")]}
    assert route_table_of(template, "AppSubnetA") != route_table_of(template, "AppSubnetB")
    assert props(template, "NatGatewayB")["SubnetId"] == ref("PublicSubnetB")


def test_db_subnets_have_no_internet_route(template):
    for subnet in DB:
        assert default_route(template, route_table_of(template, subnet)) is None


def test_every_subnet_has_a_custom_nacl(template):
    associated = {a["Properties"]["SubnetId"]["Ref"]: a["Properties"]["NetworkAclId"]["Ref"]
                  for a in of_type(template, "AWS::EC2::SubnetNetworkAclAssociation").values()}
    assert associated == {**dict.fromkeys(PUBLIC, "PublicNacl"), **dict.fromkeys(APP, "AppNacl"),
                          **dict.fromkeys(DB, "DbNacl")}


def test_db_nacl_admits_postgres_only_from_app_subnets(template):
    inbound = [e["Properties"] for e in of_type(template, "AWS::EC2::NetworkAclEntry").values()
               if e["Properties"]["NetworkAclId"] == ref("DbNacl") and not e["Properties"].get("Egress")]
    app_cidrs = [getatt(s, "CidrBlock") for s in APP]
    db_cidrs = [getatt(s, "CidrBlock") for s in DB]
    for entry in inbound:
        assert entry["CidrBlock"] in app_cidrs + db_cidrs
        if entry["CidrBlock"] in app_cidrs:
            assert entry["PortRange"] == {"From": 5432, "To": 5432}
    assert {json.dumps(e["CidrBlock"]) for e in inbound if e.get("PortRange", {}).get("From") == 5432} == \
        {json.dumps(c) for c in app_cidrs}


# ---------------------------------------------------------------- security groups
def test_security_groups_form_a_chain(template):
    assert all(r.get("CidrIp") == "0.0.0.0/0" for r in sg_rules(template, "AlbSecurityGroup", "ingress"))
    assert sg_rules(template, "AlbSecurityGroup", "egress") == [
        {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
         "DestinationSecurityGroupId": ref("AppSecurityGroup"), "Description": "To app"}]
    app_in = sg_rules(template, "AppSecurityGroup", "ingress")
    assert app_in and all(r.get("SourceSecurityGroupId") == ref("AlbSecurityGroup") for r in app_in)
    db_in = sg_rules(template, "DatabaseSecurityGroup", "ingress")
    assert db_in and all(r.get("SourceSecurityGroupId") == ref("AppSecurityGroup") and r["FromPort"] == 5432
                         for r in db_in)


def test_nothing_opens_ssh(template):
    for name in of_type(template, "AWS::EC2::SecurityGroup"):
        for rule in sg_rules(template, name, "ingress"):
            low, high = rule.get("FromPort", 0), rule.get("ToPort", 65535)
            assert rule.get("IpProtocol") != "-1" and not low <= 22 <= high, (name, rule)


# ---------------------------------------------------------------- compute
def test_instances_are_private_and_hardened(template):
    asg = props(template, "AutoScalingGroup")
    assert asg["VPCZoneIdentifier"] == [ref(s) for s in APP]
    assert asg["HealthCheckType"] == "ELB"
    data = props(template, "LaunchTemplate")["LaunchTemplateData"]
    assert "KeyName" not in data and "NetworkInterfaces" not in data
    assert data["MetadataOptions"]["HttpTokens"] == "required"
    assert all(b["Ebs"]["Encrypted"] is True for b in data["BlockDeviceMappings"])
    assert template["Parameters"]["AsgMinSize"]["MinValue"] >= 2


def test_session_manager_replaces_bastion(template):
    policies = props(template, "InstanceRole")["ManagedPolicyArns"]
    assert {"Fn::Sub": "arn:${AWS::Partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"} in policies
    assert not [n for n, r in of_type(template, "AWS::EC2::Instance").items()], "no bastion host"


def test_scaling_uses_target_tracking_and_step_policies(template):
    policies = of_type(template, "AWS::AutoScaling::ScalingPolicy")
    tracked = {p["Properties"]["TargetTrackingConfiguration"]["PredefinedMetricSpecification"]["PredefinedMetricType"]
               for p in policies.values() if p["Properties"]["PolicyType"] == "TargetTrackingScaling"}
    assert tracked == {"ASGAverageCPUUtilization", "ALBRequestCountPerTarget"}
    steps = [n for n, p in policies.items() if p["Properties"]["PolicyType"] == "StepScaling"]
    assert steps == ["CpuStepScaleOutPolicy"]
    assert ref("CpuStepScaleOutPolicy") in props(template, "CpuHighAlarm")["AlarmActions"]
    assert all(p["Properties"]["AutoScalingGroupName"] == ref("AutoScalingGroup") for p in policies.values())


# ---------------------------------------------------------------- load balancer and WAF
def test_alb_is_public_with_health_checked_targets(template):
    alb = props(template, "ALB")
    assert alb["Scheme"] == "internet-facing" and alb["Subnets"] == [ref(s) for s in PUBLIC]
    tg = props(template, "TargetGroup")
    assert tg["HealthCheckPath"] == "/health" and tg["Matcher"] == {"HttpCode": "200"}
    assert props(template, "HttpsListener")["SslPolicy"].startswith("ELBSecurityPolicy-TLS13")


def test_listener_rules_route_static_to_cloudfront_and_block_admin(template):
    rules = {n: r["Properties"] for n, r in of_type(template, "AWS::ElasticLoadBalancingV2::ListenerRule").items()}
    assert {r["Actions"][0]["Type"] for r in rules.values()} == {"redirect", "fixed-response"}
    redirect = rules["StaticRedirectRule"]["Actions"][0]["RedirectConfig"]
    assert redirect["Host"] == getatt("CloudFrontDistribution", "DomainName") and redirect["Protocol"] == "HTTPS"
    assert rules["AdminBlockRule"]["Actions"][0]["FixedResponseConfig"]["StatusCode"] == "403"
    assert len({r["Priority"] for r in rules.values()}) == len(rules)


def test_waf_protects_the_alb_with_owasp_rule_groups(template):
    acl = props(template, "WebACL")
    assert acl["Scope"] == "REGIONAL"
    groups = {r["Statement"]["ManagedRuleGroupStatement"]["Name"] for r in acl["Rules"]
              if "ManagedRuleGroupStatement" in r["Statement"]}
    assert {"AWSManagedRulesCommonRuleSet", "AWSManagedRulesKnownBadInputsRuleSet",
            "AWSManagedRulesSQLiRuleSet"} <= groups
    assert any("RateBasedStatement" in r["Statement"] for r in acl["Rules"])
    assert len({r["Priority"] for r in acl["Rules"]}) == len(acl["Rules"])
    assert props(template, "WebACLAssociation")["ResourceArn"] == ref("ALB")
    assert props(template, "WafLogGroup")["LogGroupName"]["Fn::Sub"].startswith("aws-waf-logs-")


# ---------------------------------------------------------------- data
def test_database_is_multi_az_private_and_encrypted(template):
    db = template["Resources"]["Database"]
    p = db["Properties"]
    assert p["MultiAZ"] is True and p["PubliclyAccessible"] is False and p["StorageEncrypted"] is True
    assert p["ManageMasterUserPassword"] is True and "MasterUserPassword" not in p
    assert db["DeletionPolicy"] == "Snapshot" and db["UpdateReplacePolicy"] == "Snapshot"
    assert props(template, "DBSubnetGroup")["SubnetIds"] == [ref(s) for s in DB]


def test_static_bucket_is_private_and_readable_only_by_the_distribution(template):
    block = props(template, "StaticBucket")["PublicAccessBlockConfiguration"]
    assert all(block[k] is True for k in ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets"))
    origin = props(template, "CloudFrontDistribution")["DistributionConfig"]["Origins"][0]
    assert origin["OriginAccessControlId"] == getatt("StaticOriginAccessControl", "Id")
    allow = props(template, "StaticBucketPolicy")["PolicyDocument"]["Statement"][0]
    assert allow["Principal"] == {"Service": "cloudfront.amazonaws.com"}
    assert "${CloudFrontDistribution}" in allow["Condition"]["StringEquals"]["AWS:SourceArn"]["Fn::Sub"]


# ---------------------------------------------------------------- DNS and monitoring
def test_route53_alias_and_health_check(template):
    record = template["Resources"]["AppDnsRecord"]
    assert record["Condition"] == "HasDomain"
    assert record["Properties"]["AliasTarget"]["DNSName"] == getatt("ALB", "DNSName")
    assert props(template, "HealthCheck")["HealthCheckConfig"]["ResourcePath"] == "/health"
    assert template["Resources"]["HealthCheckAlarm"]["Condition"] == "IsUsEast1"


def test_every_alarm_notifies_sns(template):
    alarms = of_type(template, "AWS::CloudWatch::Alarm")
    assert len(alarms) >= 8
    for name, alarm in alarms.items():
        assert ref("AlertsTopic") in alarm["Properties"]["AlarmActions"], name


def test_dashboard_body_is_valid_json(template):
    body = json.loads(render_sub(props(template, "Dashboard")["DashboardBody"]))
    assert len(body["widgets"]) >= 8
    assert all(w["properties"]["metrics"] for w in body["widgets"])


# ---------------------------------------------------------------- user data and app
def test_user_data_fits_ec2_limit(user_data):
    assert len(user_data.encode()) < 16 * 1024


def test_cloudwatch_agent_config_is_valid_and_escaped(user_data):
    config = json.loads(heredoc(user_data, "/opt/aws/amazon-cloudwatch-agent/etc/webapp.json"))
    # ${!aws:...} in Fn::Sub must render as the agent's own ${aws:...} placeholder.
    assert config["metrics"]["append_dimensions"]["AutoScalingGroupName"] == "${aws:AutoScalingGroupName}"
    assert config["logs"]["logs_collected"]["files"]["collect_list"][0]["file_path"] == "/var/log/webapp/app.log"


def test_embedded_app_is_python39_compatible(user_data):
    # Amazon Linux 2023 ships Python 3.9.
    ast.parse(heredoc(user_data, "/opt/webapp/app.py"), feature_version=(3, 9))


@pytest.fixture
def running_app(tmp_path, user_data):
    app = tmp_path / "app.py"
    app.write_text(heredoc(user_data, "/opt/webapp/app.py"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "HOST": "127.0.0.1", "PORT": str(port), "STATIC_BASE_URL": "https://cdn.example/static",
           "DB_HOST": "127.0.0.1", "DB_PORT": "1", "DB_NAME": "appdb", "DB_SECRET_ARN": "unused"}
    proc = subprocess.Popen([sys.executable, "-I", str(app)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("app did not start")
    yield base
    proc.terminate()
    proc.wait(5)


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


def test_app_serves_health_page_and_degrades_without_db(running_app):
    assert fetch(running_app + "/health") == (200, "ok")
    status, page = fetch(running_app + "/")
    assert status == 200 and "Availability Zone" in page
    assert 'href="https://cdn.example/static/style.css"' in page
    status, body = fetch(running_app + "/db")
    assert status == 503 and json.loads(body)["status"] == "error"
    assert fetch(running_app + "/nope")[0] == 404


# ---------------------------------------------------------------- create-time ordering
def depends_on(template, name):
    value = template["Resources"][name].get("DependsOn", [])
    return {value} if isinstance(value, str) else set(value)


def test_create_order_avoids_known_races(template):
    # Internet-facing load balancers need the internet gateway attached first.
    assert "VpcGatewayAttachment" in depends_on(template, "ALB")
    # Instances install packages at boot: NAT routes and subnet associations must be in effect.
    assert {"AppDefaultRouteA", "AppDefaultRouteB", "AppSubnetARouteTable", "AppSubnetBRouteTable"} <= \
        depends_on(template, "AutoScalingGroup")
    # ALBRequestCountPerTarget needs the target group attached to the serving listener (HTTP or HTTPS).
    assert {"HttpListener", "StaticRedirectRule"} <= depends_on(template, "RequestCountTargetTrackingPolicy")
    assert props(template, "StaticRedirectRule")["ListenerArn"] == \
        {"Fn::If": ["HasDomain", ref("HttpsListener"), ref("HttpListener")]}


@pytest.mark.parametrize("name,ok", [("webapp", True), ("web-app-1", True), ("ab", True),
                                     ("app-", False), ("a--b", False), ("internal-x", False),
                                     ("1app", False), ("Webapp", False)])
def test_project_name_pattern_rejects_names_rds_or_elb_would_refuse(template, name, ok):
    pattern = template["Parameters"]["ProjectName"]["AllowedPattern"]
    assert bool(re.fullmatch(pattern, name)) is ok
