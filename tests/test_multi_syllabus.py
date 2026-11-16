"""Tests for Full-Curriculum / Multi-Subject Syllabus Upload & Auto-Detection."""
import io
import pytest
from datetime import date
from starlette.testclient import TestClient
from app.main import app
from app.db import init_db, seed_demo_data

USER_CURRICULUM_TEXT = """Blockchain Technology

1
Introduction to DLT and Blockchain
1.1 Introduction to Blockchain: Technical definition of Blockchain, Elements and Features of Blockchain, Types of Blockchain
1.2 Distributed Ledger Technology (DLT) and comparison with Blockchain, CAP Theorem and Byzantine Generals Problem, Consensus Mechanisms and Types, Cryptographic primitives and Data Structures used in Blockchain
1.3 Hash Functions and Puzzle-Friendly Property of Hash, Structure of a Block, Block Header, Hash and Block Height, Genesis Block and Linking of Blocks, Merkle Tree Self Learning Topics: Blockchain Demonstration, Study of public and private blockchain platforms

2
Bitcoin and Consensus Mechanisms
2.1 History and Fundamentals of Bitcoin, Bitcoin Transactions and UTXO Model, Bitcoin Keys, Addresses and Wallets, HD Wallets from Seed
2.2 Transaction Scripts and Script Addresses, Pay-to-Script Hash (P2SH), Multisig and Vanity Addresses, Bitcoin Mining and Difficulty Levels
2.3 Structure of Bitcoin Blocks and Block Header, Bitcoin Network Architecture, Node Types: Full Nodes and SPV Nodes, Peer-to-Peer Network, Relay Networks and Network Discovery, Incentive-based Engineering.
Self Learning Topics: Base58, BIP-38

Big Data Analytics
1
Introduction to Big Data
1.1 Introduction to Big Data, Big Data characteristics, types of Big Data.
1.2 Traditional vs. Big Data business approach, Big Data Challenges.
1.3 Examples of Big Data in Real Life, Big Data Applications.
Self Learning Topics: Identification of Big Data applications and its solutions.

Data Science
1
Introduction to Data Science
1.1 Introduction to Data Science; Data Science Process (CRISP-DM); Types of data – structured, semi-structured, unstructured (text, logs, JSON).

Generative AI
1
Foundations of NLP and Large Language Models
1.1 Introduction: What is Natural Language Processing? Origins of NLP, Language and Knowledge,The Challenges of NLP.
"""

@pytest.fixture(autouse=True)
def setup():
    init_db()
    seed_demo_data(force=True)

def test_api_syllabus_bulk_text():
    client = TestClient(app)
    res = client.post("/api/syllabus/bulk", data={"text": USER_CURRICULUM_TEXT})
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["subjects_count"] == 4
    assert data["total_topics"] >= 10
    names = [s["name"] for s in data["subjects"]]
    assert "Blockchain Technology" in names
    assert "Big Data Analytics" in names
    assert "Data Science" in names
    assert "Generative AI" in names
    for s in data["subjects"]:
        assert len(s["topics"]) > 0
        assert s["exam_date"] > date.today().isoformat()
        assert s["color"].startswith("#")

def test_api_syllabus_bulk_file_upload():
    client = TestClient(app)
    file_bytes = io.BytesIO(USER_CURRICULUM_TEXT.encode("utf-8"))
    files = {"file": ("my_syllabus.txt", file_bytes, "text/plain")}
    res = client.post("/api/syllabus/bulk", files=files)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["subjects_count"] == 4

def test_api_syllabus_export_json():
    client = TestClient(app)
    res = client.get("/api/syllabus/export-json")
    assert res.status_code == 200
    data = res.json()
    assert "subjects" in data
    assert len(data["subjects"]) > 0
