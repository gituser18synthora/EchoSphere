"""Shared pagination / search / sort query parameters."""

from dataclasses import dataclass

from fastapi import Query


@dataclass
class PageParams:
    page: int
    page_size: int
    search: str | None
    sort_by: str | None
    sort_dir: str

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def page_params(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200, alias="pageSize"),
    search: str | None = Query(None, max_length=200),
    sort_by: str | None = Query(None, alias="sortBy", max_length=50),
    sort_dir: str = Query("desc", alias="sortDir", pattern="^(asc|desc)$"),
) -> PageParams:
    return PageParams(page=page, page_size=page_size, search=search, sort_by=sort_by, sort_dir=sort_dir)


def apply_sort(stmt, model, params: PageParams, allowed: dict[str, object], default):
    col = allowed.get(params.sort_by or "", default)
    return stmt.order_by(col.desc() if params.sort_dir == "desc" else col.asc())
