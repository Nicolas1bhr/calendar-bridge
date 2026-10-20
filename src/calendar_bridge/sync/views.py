"""Read the stored observation of one item, per provider, as canonical events.

A view is ``present`` (exists, converted), ``deleted`` (confirmed deletion evidence),
``absent`` (the item has no counterpart there yet) or ``unknown`` (no trustworthy information,
e.g. a resource whose disappearance is not yet verified). The engine never acts on ``unknown``.
"""

from __future__ import annotations

import dataclasses

from calendar_bridge.db.repo import GoogleEventRow, ICloudResourceRow, ItemRow, Repo
from calendar_bridge.model.canonical import CanonicalEvent, ConversionResult
from calendar_bridge.providers.google.convert import google_to_canonical
from calendar_bridge.providers.icloud.convert import ICloudConversion, ICSParseError, icloud_to_canonical
from calendar_bridge.sync.context import MappingContext

PRESENT, DELETED, ABSENT, UNKNOWN = "present", "deleted", "absent", "unknown"


@dataclasses.dataclass
class GoogleView:
    state: str
    master: GoogleEventRow | None = None
    instances: list[GoogleEventRow] = dataclasses.field(default_factory=list)
    conversion: ConversionResult | None = None

    @property
    def event(self) -> CanonicalEvent | None:
        return self.conversion.event if self.conversion and self.state == PRESENT else None

    @property
    def supported(self) -> bool:
        return self.conversion is not None and self.conversion.supported

    def instance_by_key(self) -> dict[str, GoogleEventRow]:
        return {row.recurrence_key: row for row in self.instances if row.recurrence_key}


@dataclasses.dataclass
class ICloudView:
    state: str
    row: ICloudResourceRow | None = None
    conversion: ICloudConversion | None = None

    @property
    def event(self) -> CanonicalEvent | None:
        return self.conversion.event if self.conversion and self.state == PRESENT else None

    @property
    def supported(self) -> bool:
        return self.conversion is not None and self.conversion.supported


def google_view_from_rows(
    mc: MappingContext, master: GoogleEventRow | None, instances: list[GoogleEventRow]
) -> GoogleView:
    if master is None:
        return GoogleView(UNKNOWN)
    if master.status == "cancelled":
        return GoogleView(DELETED, master=master, instances=instances)
    conversion = google_to_canonical(master.raw, [i.raw for i in instances], mc.gctx)
    return GoogleView(PRESENT, master=master, instances=instances, conversion=conversion)


def google_view(repo: Repo, mc: MappingContext, item: ItemRow) -> GoogleView:
    if item.google_event_id is None:
        return GoogleView(ABSENT)
    master, instances = repo.google_family(mc.id, item.google_event_id)
    return google_view_from_rows(mc, master, instances)


def icloud_view_from_row(mc: MappingContext, row: ICloudResourceRow | None) -> ICloudView:
    if row is None or row.status == "unverified_missing":
        return ICloudView(UNKNOWN, row=row)
    if row.status == "deleted":
        return ICloudView(DELETED, row=row)
    try:
        conversion = icloud_to_canonical(row.ics or "", mc.ictx)
    except ICSParseError as exc:
        conversion = ICloudConversion(event=None, unsupported=[str(exc)])
    return ICloudView(PRESENT, row=row, conversion=conversion)


def icloud_view(repo: Repo, mc: MappingContext, item: ItemRow) -> ICloudView:
    if item.icloud_href is None:
        return ICloudView(ABSENT)
    return icloud_view_from_row(mc, repo.icloud_resource(mc.id, item.icloud_href))
