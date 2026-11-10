"""Read-only directory lookups, independent of the chat catalog and never cached.

Public methods validate external inputs. Internal directory calls receive canonical object ids or a
bounded plain query; adapters own OData quoting. Ambiguous searches return candidates, never a choice.
"""

from uuid import UUID

from teams_connector.domain.errors import InvalidRequest
from teams_connector.domain.models import ManagerChain, OrgPeople, PersonMatches
from teams_connector.remote.ports import OrgDirectory


class Organization:
    def __init__(self, directory: OrgDirectory) -> None:
        self._directory = directory

    async def find_person(self, query: str, *, limit: int = 25) -> PersonMatches:
        """Entry point: a name prefix, exact email/UPN or object id; return all bounded candidates."""
        query = _query(query)
        if not 1 <= limit <= 100:
            raise InvalidRequest("limit must be between 1 and 100.")
        people, complete = await self._directory.find(query, limit=limit)
        return PersonMatches(items=people, complete=complete)

    async def get_manager_chain(self, person_id: str) -> ManagerChain:
        """Entry point: an Entra object id. Managers are immediate first, root last."""
        person, managers = await self._directory.manager_chain(_object_id(person_id))
        return ManagerChain(person=person, managers=managers)

    async def list_direct_reports(self, person_id: str) -> OrgPeople:
        """Entry point: an Entra object id. Resolve the person before interpreting an empty report list."""
        person_id = _object_id(person_id)
        person = await self._directory.person(person_id)
        people, complete = await self._directory.direct_reports(person.id)
        return OrgPeople(person=person, items=people, complete=complete, derived=False)

    async def list_peers(self, person_id: str) -> OrgPeople:
        """Entry point: an Entra object id. Derive peers from the current manager's user reports."""
        person_id = _object_id(person_id)
        person = await self._directory.person(person_id)
        manager = await self._directory.manager(person.id)
        if manager is None:
            return OrgPeople(
                person=person,
                items=[],
                complete=True,
                derived=True,
                reason="No manager is assigned; peers cannot be derived.",
            )
        people, complete = await self._directory.direct_reports(manager.id)
        return OrgPeople(
            person=person,
            manager=manager,
            items=[peer for peer in people if peer.id.casefold() != person.id.casefold()],
            complete=complete,
            derived=True,
        )


def _query(value: str) -> str:
    if not isinstance(value, str) or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise InvalidRequest("query must be plain text without control characters.")
    value = value.strip()
    if not 1 <= len(value) <= 200:
        raise InvalidRequest("query must contain 1 to 200 characters.")
    return value


def _object_id(value: str) -> str:
    try:
        return str(UUID(value.strip()))
    except ValueError, AttributeError:
        raise InvalidRequest(
            "person_id must be an Entra object id from find_person or an org lookup."
        ) from None
