"""Migration f5b7d9e1a3c5: the rest of Sarvam's bulbul:v4-flash roster.

Insert-only on a long-lived database (the 36 personas of c2e4a6b8d0f1 and an
operator's edits stay as they are), idempotent, identical to the seed, and
reversible to exactly the rows it added.
"""

import importlib
import json

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from backend.seeds.provider_catalog_seed import sarvam_v4_roster_voices

module = importlib.import_module(
    "backend.alembic.versions.f5b7d9e1a3c5_sarvam_v4_flash_full_persona_roster")


def _engine():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "create table voice_profiles (id varchar(40) primary key, source varchar(20), "
            "name varchar(100), gender varchar(10), languages text, locale varchar(15), "
            "accent varchar(100), styles text, latency_ms int, premium int, sample_text text, "
            "provider varchar(100), provider_voice_id varchar(100), speaking_rate float, "
            "pitch float, model_codes text, provider_settings text, is_default int, "
            "status varchar(20), sort_order int, created_at datetime, updated_at datetime, "
            "is_deleted int default 0)"))
        # A long-lived database: a v3 voice, a c2e4 persona, and one roster
        # persona an operator already added and relabelled by hand.
        conn.execute(sa.text(
            "insert into voice_profiles (id, name, provider, provider_voice_id, model_codes, gender) "
            "values ('vp-sv-shubh', 'Shubh', 'sarvam', 'shubh', '[\"bulbul:v3\"]', 'male'), "
            "('vp-sv4-shubh_hi_customer', 'Shubh · Hindi · Customer', 'sarvam', "
            "'shubh_hi_customer', '[\"bulbul:v4-flash\"]', 'male'), "
            "('vp-sv4-aarti_hi_customer', 'Aarti (ops)', 'sarvam', 'aarti_hi_customer', "
            "'[\"bulbul:v4-flash\"]', 'female')"))
    return engine


def _run(engine, step):
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            step()


def test_snapshot_matches_the_seed():
    seeded = [
        (v["id"], v["name"], v["gender"], tuple(v["languages"]), v["style"], v["provider_voice_id"])
        for v in sarvam_v4_roster_voices()
    ]
    assert list(module._ROSTER_VOICES) == seeded
    assert len(seeded) == 186
    assert max(len(row[0]) for row in seeded) <= 40  # voice_profiles.id is varchar(40)


def test_upgrade_inserts_missing_personas_only_and_is_idempotent():
    engine = _engine()
    _run(engine, module.upgrade)
    _run(engine, module.upgrade)
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "select id, name, gender, languages, locale, model_codes, provider_voice_id, "
            "status, is_default from voice_profiles where id like 'vp-sv4-%'")).all()
        by_id = {r[0]: r for r in rows}
        # 186 roster ids, one of them pre-existing, plus the c2e4 persona.
        assert len(rows) == 186 + 1
        assert by_id["vp-sv4-aarti_hi_customer"][1:3] == ("Aarti (ops)", "female")  # untouched
        assert by_id["vp-sv4-shubh_hi_customer"][1] == "Shubh · Hindi · Customer"
        tamil = by_id["vp-sv4-aravind_ta_ads"]
        assert json.loads(tamil[3]) == ["ta-IN"] and tamil[4] == "ta-IN"
        assert json.loads(tamil[5]) == ["bulbul:v4-flash"] and tamil[6] == "aravind_ta_ads"
        assert tamil[7:] == ("active", 0)
        hinglish = by_id["vp-sv4-shalini_enhi_customer"]
        assert json.loads(hinglish[3]) == ["hi-IN", "en-IN"] and hinglish[2] == "female"
        assert conn.execute(sa.text(
            "select model_codes from voice_profiles where id = 'vp-sv-shubh'")).scalar() == '["bulbul:v3"]'


def test_downgrade_removes_exactly_the_roster_rows():
    engine = _engine()
    _run(engine, module.upgrade)
    _run(engine, module.downgrade)
    with engine.connect() as conn:
        left = {r[0] for r in conn.execute(sa.text("select id from voice_profiles")).all()}
    # The operator-added roster row shares an id with the snapshot, so it goes too —
    # documented in the migration: downgrade only after moving bots off these voices.
    assert left == {"vp-sv-shubh", "vp-sv4-shubh_hi_customer"}
