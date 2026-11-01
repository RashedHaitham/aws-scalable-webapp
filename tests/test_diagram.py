"""The architecture diagram is a well-formed draw.io file that shows every required component."""
import html
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

DIAGRAM = Path(__file__).resolve().parents[1] / "diagrams" / "architecture.drawio"
REQUIRED_LABELS = [
    "Route 53", "CloudFront", "AWS WAF", "Application Load Balancer", "Auto Scaling group", "EC2",
    "NAT gateway", "Internet gateway", "RDS PostgreSQL", "standby", "Amazon S3", "Session Manager",
    "CloudWatch", "Amazon SNS", "Secrets Manager", "Certificate Manager", "Public subnet", "Private app subnet",
    "Private data subnet", "Availability Zone A", "Availability Zone B", "NACL", "VPC",
]


@pytest.fixture(scope="module")
def cells():
    root = ET.parse(DIAGRAM).getroot()
    return root.findall(".//mxCell")


def test_cell_ids_are_unique(cells):
    ids = [c.get("id") for c in cells]
    assert len(ids) == len(set(ids))


def test_edges_connect_existing_cells(cells):
    ids = {c.get("id") for c in cells}
    edges = [c for c in cells if c.get("edge") == "1"]
    assert len(edges) >= 10
    for e in edges:
        assert e.get("source") in ids and e.get("target") in ids, e.get("id")


def test_parents_exist(cells):
    ids = {c.get("id") for c in cells}
    assert all(c.get("parent") in ids for c in cells if c.get("parent"))


def test_required_components_are_labelled(cells):
    text = " ".join(html.unescape(c.get("value", "")) for c in cells)
    missing = [label for label in REQUIRED_LABELS if label not in text]
    assert not missing


def test_multi_az_placement(cells):
    by_id = {c.get("id"): c for c in cells}

    def az_of(cid):
        while cid and not cid.startswith("az_"):
            cid = by_id[cid].get("parent")
        return cid

    assert {az_of("rds_primary"), az_of("rds_standby")} == {"az_a", "az_b"}
    assert {az_of("nat_a"), az_of("nat_b")} == {"az_a", "az_b"}
    # The Auto Scaling group is drawn at VPC level because it spans both AZs.
    assert by_id["asg"].get("parent") == "vpc"
