"""Asia country master and structured Data Region country reference.

Revision ID: d4e6f8a0b2c4
Revises: a3c5e7f9b1d3
Create Date: 2026-07-22

Creates an Asia-only country catalog and links ``data_regions`` to it through
the stable ISO alpha-2 country code. The existing country-name column remains
as a compatibility/display snapshot.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e6f8a0b2c4"
down_revision: Union[str, None] = "a3c5e7f9b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ID_LEN = 40

ASIA_COUNTRIES = [
    ("af", "Afghanistan"), ("am", "Armenia"), ("az", "Azerbaijan"),
    ("bh", "Bahrain"), ("bd", "Bangladesh"), ("bt", "Bhutan"),
    ("bn", "Brunei"), ("kh", "Cambodia"), ("cn", "China"),
    ("cy", "Cyprus"), ("ge", "Georgia"), ("in", "India"),
    ("id", "Indonesia"), ("ir", "Iran"), ("iq", "Iraq"),
    ("il", "Israel"), ("jp", "Japan"), ("jo", "Jordan"),
    ("kz", "Kazakhstan"), ("kw", "Kuwait"), ("kg", "Kyrgyzstan"),
    ("la", "Laos"), ("lb", "Lebanon"), ("my", "Malaysia"),
    ("mv", "Maldives"), ("mn", "Mongolia"), ("mm", "Myanmar"),
    ("np", "Nepal"), ("kp", "North Korea"), ("om", "Oman"),
    ("pk", "Pakistan"), ("ps", "Palestine"), ("ph", "Philippines"),
    ("qa", "Qatar"), ("sa", "Saudi Arabia"), ("sg", "Singapore"),
    ("kr", "South Korea"), ("lk", "Sri Lanka"), ("sy", "Syria"),
    ("tw", "Taiwan"), ("tj", "Tajikistan"), ("th", "Thailand"),
    ("tl", "Timor-Leste"), ("tr", "Türkiye"), ("tm", "Turkmenistan"),
    ("ae", "United Arab Emirates"), ("uz", "Uzbekistan"),
    ("vn", "Vietnam"), ("ye", "Yemen"),
]


def upgrade() -> None:
    countries = op.create_table(
        "countries",
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
    op.create_index("ix_countries_status", "countries", ["status"])
    op.create_index("ix_countries_sort_order", "countries", ["sort_order"])

    op.bulk_insert(countries, [
        {
            "id": f"ctry_seed_{code}", "code": code, "name": name,
            "region": "Asia", "status": "active", "sort_order": index,
        }
        for index, (code, name) in enumerate(ASIA_COUNTRIES)
    ])

    op.add_column("data_regions", sa.Column("country_code", sa.String(2), nullable=True))
    op.create_index("ix_data_regions_country_code", "data_regions", ["country_code"])

    # Backfill legacy name/code values and normalize matched records to Asia.
    op.execute(sa.text("""
        UPDATE data_regions AS dr
        INNER JOIN countries AS c
          ON LOWER(TRIM(dr.country)) = LOWER(c.name)
          OR LOWER(TRIM(dr.country)) = c.code
        SET dr.country_code = c.code,
            dr.country = c.name,
            dr.region = c.region
        WHERE dr.country IS NOT NULL
    """))

    op.create_foreign_key(
        "fk_data_regions_country_code",
        "data_regions", "countries", ["country_code"], ["code"],
        ondelete="RESTRICT", onupdate="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_data_regions_country_code", "data_regions", type_="foreignkey")
    op.drop_index("ix_data_regions_country_code", table_name="data_regions")
    op.drop_column("data_regions", "country_code")
    op.drop_index("ix_countries_sort_order", table_name="countries")
    op.drop_index("ix_countries_status", table_name="countries")
    op.drop_table("countries")
