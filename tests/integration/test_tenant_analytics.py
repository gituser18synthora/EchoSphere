"""Tenant analytics calculations backed by isolated control-plane rows."""

from datetime import date, datetime, time, timedelta
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from backend.core.security import create_access_token
from backend.main import app
from shared.db.mysql import get_sessionmaker
from shared.ids import new_id
from shared.models import ConversationSession, Role, Tenant, UsageRecord, User, VoiceBot


pytestmark = pytest.mark.integration

API = "/api/v1"


@pytest.fixture()
def analytics_tenant():
    suffix = uuid.uuid4().hex[:10]
    session = get_sessionmaker()()
    try:
        role = session.execute(select(Role).where(Role.code == "tenant_admin")).scalar_one()
        tenant = Tenant(
            id=new_id("tn"), name=f"Analytics Test {suffix}", code=f"analytics_{suffix}",
            domain=f"analytics-{suffix}.example.test", status="active",
        )
        session.add(tenant)
        session.flush()
        user = User(
            id=new_id("usr"), email=f"analytics.{suffix}@example.test",
            name="Analytics Admin", password_hash="x", role_id=role.id,
            tenant_id=tenant.id, status="active",
        )
        bot = VoiceBot(
            id=new_id("bot"), tenant_id=tenant.id, name="CSAT Test Bot",
            status="published", owner_user_id=user.id,
        )
        unrated_bot = VoiceBot(
            id=new_id("bot"), tenant_id=tenant.id, name="Unrated Bot",
            status="published", owner_user_id=user.id,
        )
        session.add_all([user, bot, unrated_bot])
        session.flush()

        today = date.today()
        yesterday = today - timedelta(days=1)
        session.add_all([
            UsageRecord(
                id=new_id("ur"), tenant_id=tenant.id, date=yesterday,
                calls=10, csat_avg=1.0,
            ),
            UsageRecord(
                id=new_id("ur"), tenant_id=tenant.id, date=today,
                calls=10, csat_avg=5.0,
            ),
        ])

        def conversation(*, bot_id: str, day: date, csat: int | None) -> ConversationSession:
            return ConversationSession(
                id=new_id("cv"), tenant_id=tenant.id, bot_id=bot_id,
                started_at=datetime.combine(day, time(hour=12)), csat=csat,
                sentiment="neutral", intents=[], contained=True, status="completed",
            )

        # A simple average of the two daily averages would be 3.0. The correct
        # rating-weighted average is (5 + 1 + 1 + 1) / 4 = 2.0.
        session.add(conversation(bot_id=bot.id, day=yesterday, csat=5))
        session.add_all([
            conversation(bot_id=bot.id, day=today, csat=1),
            conversation(bot_id=bot.id, day=today, csat=1),
            conversation(bot_id=bot.id, day=today, csat=1),
            conversation(bot_id=bot.id, day=today, csat=None),
            conversation(bot_id=unrated_bot.id, day=today, csat=None),
        ])
        session.commit()

        yield {
            "tenant_id": tenant.id,
            "bot_id": bot.id,
            "unrated_bot_id": unrated_bot.id,
            "headers": {
                "Authorization": f"Bearer {create_access_token(user_id=user.id, role='tenant_admin', tenant_id=tenant.id)}"
            },
        }
    finally:
        tenant_id = tenant.id
        session.rollback()
        session.execute(delete(ConversationSession).where(ConversationSession.tenant_id == tenant_id))
        session.execute(delete(UsageRecord).where(UsageRecord.tenant_id == tenant_id))
        session.execute(delete(VoiceBot).where(VoiceBot.tenant_id == tenant_id))
        session.execute(delete(User).where(User.tenant_id == tenant_id))
        session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        session.commit()
        session.close()


def _kpi(payload: dict, label: str) -> dict:
    return next(kpi for kpi in payload["data"]["kpis"] if kpi["label"] == label)


def test_avg_csat_uses_actual_ratings_and_rating_count(analytics_tenant):
    with TestClient(app) as client:
        response = client.get(
            f"{API}/analytics/tenant?days=30&botId={analytics_tenant['bot_id']}",
            headers=analytics_tenant["headers"],
        )

    assert response.status_code == 200, response.text
    csat = _kpi(response.json(), "Avg CSAT")
    assert csat["value"] == "2.0 / 5"
    assert csat["spark"][-2:] == [50, 10]


def test_avg_csat_ignores_unrated_conversations(analytics_tenant):
    with TestClient(app) as client:
        response = client.get(
            f"{API}/analytics/tenant?days=30&botId={analytics_tenant['unrated_bot_id']}",
            headers=analytics_tenant["headers"],
        )

    assert response.status_code == 200, response.text
    csat = _kpi(response.json(), "Avg CSAT")
    assert csat["value"] == "—"
    assert csat["spark"] == [0] * 14
