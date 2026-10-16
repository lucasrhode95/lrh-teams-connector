# Teams connector documentation

| Document | Read it for |
|---|---|
| [Requirements](requirements.md) | What the connector must do, and the decisions behind it |
| [Architecture](architecture.md) | How the connector is built: layers, sign-in profiles, storage, safety rules, logging, and the reference for every MCP tool |
| [Roadmap](roadmap.md) | Open bugs, improvements, enhancements and validation |
| [UI facelift plan](ui-facelift-plan.md) | Current UI layout, preview behavior and implementation record |
| [API research](api-research.md) | Dated evidence of what Microsoft does not document: Teams' private services, the Teams client's Graph grants, and Teams Web's search routes |

Diagrams: [architecture](diagrams/architecture.svg) (overview) and [code map](diagrams/code-map.svg)
(classes and who calls whom; open it in a browser to see the tooltips).

Start with the [repository README](../README.md), then the architecture. The research is a dated
evidence log: read the section you need, not the whole file.

The [API research](api-research.md) has one section per service, listed in its [contents](api-research.md#contents). The architecture maps each tool and route to its service, ports and adapters.
