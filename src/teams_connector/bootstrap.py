"""Composition root: config → tokens → transport → adapters → store → services.

Built lazily on first use, so a starting process does no network or disk work (architecture §2).
This is the one place that decides which backend serves each capability (architecture §6).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from teams_connector.auth.tokens import Account, TokenProvider
from teams_connector.domain.errors import AuthenticationRequired
from teams_connector.domain.models import Participant
from teams_connector.remote.chat_service import ChatService
from teams_connector.remote.csa import CsaCatalog
from teams_connector.remote.csa_mapping import Me
from teams_connector.remote.graph import Graph
from teams_connector.remote.graph_teams import GraphChannels, GraphFiles, GraphPeople, GraphWriter
from teams_connector.remote.teams_session import TeamsSession
from teams_connector.remote.transport import Bearer, Transport
from teams_connector.service.archive import Archive
from teams_connector.service.catalog import Catalog
from teams_connector.service.export.orchestrator import Exports
from teams_connector.service.files import Files, Photos
from teams_connector.service.history import History
from teams_connector.service.search import Search
from teams_connector.service.send import Sender
from teams_connector.store.db import Store, store_path


@dataclass
class Services:
    account: Account
    me: Participant
    store: Store
    catalog: Catalog
    history: History
    search: Search
    archive: Archive
    exports: Exports
    sender: Sender
    files: Files
    photos: Photos


class AppContext:
    def __init__(
        self,
        *,
        unsecure: bool = False,
        tokens: TokenProvider | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.tokens = tokens or TokenProvider(unsecure=unsecure)
        self._http_client = http_client
        self._transport: Transport | None = None
        self._services: Services | None = None
        self._lock = asyncio.Lock()

    async def services(self) -> Services:
        if self._services is None:
            async with self._lock:  # concurrent first requests (UI) build the services once
                if self._services is None:
                    self._services = await self._build()
        return self._services

    async def _build(self) -> Services:
        claims = self.tokens.get_token("teams").claims()
        if not claims.get("tid") or not claims.get("oid"):
            raise AuthenticationRequired(
                "The Teams token carries no account identity.", command=self.tokens.sign_in_command()
            )
        account = Account(
            tenant_id=claims["tid"],
            object_id=str(claims["oid"]).lower(),
            username=claims.get("upn") or claims.get("preferred_username") or claims.get("unique_name"),
        )
        name = claims.get("name") if isinstance(claims.get("name"), str) else account.username
        me = Me(user_id=account.object_id, name=name)
        transport = self._transport = Transport(client=self._http_client)
        graph = Graph(transport, Bearer(self.tokens, "graph"))
        session = TeamsSession(transport, self.tokens)
        people = GraphPeople(graph)
        channels = GraphChannels(graph)
        chat_service = ChatService(transport, session, people)
        catalog_source = CsaCatalog(transport, session, self.tokens, people, chat_service, me)

        store = Store(store_path(account.fingerprint), account.fingerprint)
        catalog = Catalog(store, catalog_source, channels, chat_service)
        history = History(store, catalog, chat_service, channels, account.object_id)
        person = Participant(id=account.object_id, name=name)
        return Services(
            account=account,
            me=person,
            store=store,
            catalog=catalog,
            history=history,
            search=Search(store, catalog, history),
            archive=Archive(store, catalog, history, chat_service, channels, person),
            exports=Exports(store, catalog, history),
            sender=Sender(catalog, GraphWriter(graph)),
            files=Files(store, GraphFiles(graph)),
            photos=Photos(people),
        )

    async def aclose(self) -> None:
        if self._transport is not None:
            await self._transport.aclose()
