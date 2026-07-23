"""Numeric country IDs with ISO2 and ISO3 codes.

Revision ID: e5f7a9b1c3d5
Revises: d4e6f8a0b2c4
Create Date: 2026-07-23

Rebuilds the Asia country catalog with an auto-increment integer primary key,
renames the old two-letter ``code`` to ``iso2``, adds ``iso3``, and changes
Data Region references from the ISO2 code to the numeric country ID.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5f7a9b1c3d5"
down_revision: Union[str, None] = "d4e6f8a0b2c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ID_LEN = 40

ISO3_BY_ISO2 = {
    "AF": "AFG", "AM": "ARM", "AZ": "AZE", "BH": "BHR", "BD": "BGD",
    "BT": "BTN", "BN": "BRN", "KH": "KHM", "CN": "CHN", "CY": "CYP",
    "GE": "GEO", "IN": "IND", "ID": "IDN", "IR": "IRN", "IQ": "IRQ",
    "IL": "ISR", "JP": "JPN", "JO": "JOR", "KZ": "KAZ", "KW": "KWT",
    "KG": "KGZ", "LA": "LAO", "LB": "LBN", "MY": "MYS", "MV": "MDV",
    "MN": "MNG", "MM": "MMR", "NP": "NPL", "KP": "PRK", "OM": "OMN",
    "PK": "PAK", "PS": "PSE", "PH": "PHL", "QA": "QAT", "SA": "SAU",
    "SG": "SGP", "KR": "KOR", "LK": "LKA", "SY": "SYR", "TW": "TWN",
    "TJ": "TJK", "TH": "THA", "TL": "TLS", "TR": "TUR", "TM": "TKM",
    "AE": "ARE", "UZ": "UZB", "VN": "VNM", "YE": "YEM",
}


def _country_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("iso2", sa.String(2), nullable=False),
        sa.Column("iso3", sa.String(3), nullable=False),
        sa.Column("region", sa.String(50), nullable=False, server_default="Asia"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(ID_LEN), nullable=True),
        sa.Column("updated_by", sa.String(ID_LEN), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
        sa.Column("deleted_by", sa.String(ID_LEN), nullable=True),
        sa.UniqueConstraint("name", name="uq_countries_name"),
        sa.UniqueConstraint("iso2", name="uq_countries_iso2"),
        sa.UniqueConstraint("iso3", name="uq_countries_iso3"),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    old_rows = bind.execute(sa.text("""
        SELECT name, code, region, status, sort_order, created_at, updated_at,
               created_by, updated_by, is_deleted, deleted_at, deleted_by
        FROM countries
        ORDER BY sort_order, name, id
    """)).mappings().all()

    missing = sorted({
        str(row["code"]).upper()
        for row in old_rows
        if str(row["code"]).upper() not in ISO3_BY_ISO2
    })
    if missing:
        raise RuntimeError(
            "Cannot migrate country codes without ISO3 mappings: " + ", ".join(missing)
        )

    countries_new = op.create_table(
        "countries_new",
        *_country_columns(),
    )
    op.create_index("ix_countries_status", "countries_new", ["status"])
    op.create_index("ix_countries_sort_order", "countries_new", ["sort_order"])

    if old_rows:
        op.bulk_insert(countries_new, [
            {
                "id": index,
                "name": row["name"],
                "iso2": str(row["code"]).upper(),
                "iso3": ISO3_BY_ISO2[str(row["code"]).upper()],
                "region": row["region"],
                "status": row["status"],
                "sort_order": row["sort_order"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "created_by": row["created_by"],
                "updated_by": row["updated_by"],
                "is_deleted": row["is_deleted"],
                "deleted_at": row["deleted_at"],
                "deleted_by": row["deleted_by"],
            }
            for index, row in enumerate(old_rows, start=1)
        ])

    op.add_column("data_regions", sa.Column("country_id", sa.Integer, nullable=True))
    op.create_index("ix_data_regions_country_id", "data_regions", ["country_id"])
    op.execute(sa.text("""
        UPDATE data_regions AS dr
        INNER JOIN countries_new AS c
          ON UPPER(dr.country_code) = c.iso2
        SET dr.country_id = c.id,
            dr.country = c.name,
            dr.region = c.region
        WHERE dr.country_code IS NOT NULL
    """))

    op.drop_constraint(
        "fk_data_regions_country_code", "data_regions", type_="foreignkey"
    )
    op.drop_index("ix_data_regions_country_code", table_name="data_regions")
    op.drop_column("data_regions", "country_code")
    op.drop_table("countries")
    op.rename_table("countries_new", "countries")

    op.create_foreign_key(
        "fk_data_regions_country_id",
        "data_regions", "countries", ["country_id"], ["id"],
        ondelete="RESTRICT", onupdate="CASCADE",
    )


def downgrade() -> None:
    bind = op.get_bind()
    current_rows = bind.execute(sa.text("""
        SELECT id, name, iso2, region, status, sort_order, created_at, updated_at,
               created_by, updated_by, is_deleted, deleted_at, deleted_by
        FROM countries
        ORDER BY id
    """)).mappings().all()

    countries_legacy = op.create_table(
        "countries_legacy",
        sa.Column("id", sa.String(ID_LEN), primary_key=True),
        sa.Column("code", sa.String(2), nullable=False, unique=True),
        sa.Column("name", sa.String(150), nullable=False, unique=True),
        sa.Column("region", sa.String(50), nullable=False, server_default="Asia"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(ID_LEN), nullable=True),
        sa.Column("updated_by", sa.String(ID_LEN), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
        sa.Column("deleted_by", sa.String(ID_LEN), nullable=True),
    )
    op.create_index("ix_countries_status", "countries_legacy", ["status"])
    op.create_index("ix_countries_sort_order", "countries_legacy", ["sort_order"])

    if current_rows:
        op.bulk_insert(countries_legacy, [
            {
                "id": f"ctry_legacy_{row['id']}",
                "code": str(row["iso2"]).lower(),
                "name": row["name"],
                "region": row["region"],
                "status": row["status"],
                "sort_order": row["sort_order"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "created_by": row["created_by"],
                "updated_by": row["updated_by"],
                "is_deleted": row["is_deleted"],
                "deleted_at": row["deleted_at"],
                "deleted_by": row["deleted_by"],
            }
            for row in current_rows
        ])

    op.drop_constraint(
        "fk_data_regions_country_id", "data_regions", type_="foreignkey"
    )
    op.add_column("data_regions", sa.Column("country_code", sa.String(2), nullable=True))
    op.create_index("ix_data_regions_country_code", "data_regions", ["country_code"])
    op.execute(sa.text("""
        UPDATE data_regions AS dr
        INNER JOIN countries AS c ON dr.country_id = c.id
        SET dr.country_code = LOWER(c.iso2)
        WHERE dr.country_id IS NOT NULL
    """))
    op.drop_index("ix_data_regions_country_id", table_name="data_regions")
    op.drop_column("data_regions", "country_id")
    op.drop_table("countries")
    op.rename_table("countries_legacy", "countries")

    op.create_foreign_key(
        "fk_data_regions_country_code",
        "data_regions", "countries", ["country_code"], ["code"],
        ondelete="RESTRICT", onupdate="CASCADE",
    )
