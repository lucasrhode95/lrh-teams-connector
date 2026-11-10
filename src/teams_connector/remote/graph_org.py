"""Graph v1.0 organization reads using teams.graph, with no new scopes or private profile routes.

Callers validate plain queries, limits and object ids. This adapter quotes OData literals, maps wire
fields and follows only Graph continuation URLs. Permission failures remain failures, not empty lists.
"""

from typing import Any
from uuid import UUID

from teams_connector.domain.errors import NotFound, Upstream
from teams_connector.domain.models import Person
from teams_connector.remote.graph import Graph, segment
from teams_connector.remote.transport import operation

PROFILE = "id,displayName,mail,userPrincipalName"
MAX_REPORT_PAGES = 100
MAX_REPORTS = 10_000
MAX_MANAGERS = 1000


class GraphOrgDirectory:
    def __init__(self, graph: Graph) -> None:
        self._graph = graph

    async def find(self, query: str, *, limit: int) -> tuple[list[Person], bool]:
        with operation("finding directory people"):
            try:
                identifier = str(UUID(query))
            except ValueError:
                identifier = None
            if identifier is not None:
                try:
                    return [await self.person(identifier)], True
                except NotFound:
                    return [], True
            literal = query.replace("'", "''")
            if "@" in query:
                predicate = f"mail eq '{literal}' or userPrincipalName eq '{literal}'"
            else:
                predicate = f"startswith(displayName,'{literal}')"
            rows, truncated = await self._graph.collect(
                "/users", {"$select": PROFILE, "$filter": predicate, "$top": limit + 1}, max_items=limit + 1
            )
            return [_person(row) for row in rows[:limit]], not truncated and len(rows) <= limit

    async def person(self, person_id: str) -> Person:
        with operation("reading a directory person"):
            return _person(await self._graph.get(f"/users/{segment(person_id)}", {"$select": PROFILE}))

    async def manager_chain(self, person_id: str) -> tuple[Person, list[Person]]:
        with operation("reading a manager chain"):
            row = await self._graph.get(
                f"/users/{segment(person_id)}",
                {"$select": PROFILE, "$expand": f"manager($levels=max;$select={PROFILE})"},
                headers={"ConsistencyLevel": "eventual"},
            )
        subject = _person(row)
        managers: list[Person] = []
        seen = {subject.id}
        manager = row.get("manager")
        while manager is not None:
            if not isinstance(manager, dict):
                raise Upstream("Graph returned an invalid manager chain.")
            person = _person(manager)
            if person.id in seen or len(managers) >= MAX_MANAGERS:
                raise Upstream("Graph returned a cyclic or oversized manager chain.")
            seen.add(person.id)
            managers.append(person)
            manager = manager.get("manager")
        return subject, managers

    async def manager(self, person_id: str) -> Person | None:
        with operation("reading a directory manager"):
            try:
                row = await self._graph.get(f"/users/{segment(person_id)}/manager", {"$select": PROFILE})
            except NotFound:
                return None
        return _person(row)

    async def direct_reports(self, person_id: str) -> tuple[list[Person], bool]:
        with operation("reading direct reports"):
            path = f"/users/{segment(person_id)}/directReports"
            params = {"$select": PROFILE}
            people: dict[str, Person] = {}
            seen_links: set[str] = set()
            for _ in range(MAX_REPORT_PAGES):
                rows, link = await self._graph.page(path, params)
                for row in rows:
                    if row.get("@odata.type") == "#microsoft.graph.user":
                        person = _person(row)
                        if person.id not in people and len(people) >= MAX_REPORTS:
                            return list(people.values()), False
                        people[person.id] = person
                if link is None:
                    return list(people.values()), True
                if link in seen_links:
                    raise Upstream("Graph direct reports repeated a continuation link; pagination stopped.")
                seen_links.add(link)
                path, params = link, None
        return list(people.values()), False


def _person(row: dict[str, Any]) -> Person:
    """Map structured Graph fields; limited-information users keep their id and absent profile fields."""
    if row.get("@odata.type", "#microsoft.graph.user") != "#microsoft.graph.user":
        raise Upstream("Graph returned a non-user directory manager.")
    identifier = row.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise Upstream("Graph returned a directory person without an id.")
    return Person(
        id=identifier.lower(),
        name=_text(row.get("displayName")),
        email=_text(row.get("mail")),
        user_principal_name=_text(row.get("userPrincipalName")),
    )


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
