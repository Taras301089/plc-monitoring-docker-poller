from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Iterable

from asyncua import Client, ua


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Browse OPC UA variables exposed by a PLC"
    )
    parser.add_argument(
        "--url",
        default=os.getenv("OPC_ENDPOINT"),
        help="OPC UA endpoint, for example opc.tcp://10.85.226.35:4840",
    )
    parser.add_argument("--username", default=os.getenv("OPC_USERNAME"))
    parser.add_argument("--password", default=os.getenv("OPC_PASSWORD"))
    return parser.parse_args()


async def browse_variables(client: Client) -> None:
    visited: set[str] = set()
    objects = client.get_objects_node()

    async def visit(node, level: int = 0) -> None:
        node_key = node.nodeid.to_string()
        if node_key in visited:
            return
        visited.add(node_key)

        try:
            references = await asyncio.wait_for(
                node.get_references(
                    refs=ua.ObjectIds.HierarchicalReferences,
                    direction=ua.BrowseDirection.Forward,
                ),
                timeout=10,
            )
        except Exception as exc:
            print(f"ERROR node={node_key}: {exc}")
            return

        for reference in references:
            child_node_id = reference.NodeId.to_string()
            if child_node_id in visited:
                continue
            visited.add(child_node_id)

            child = client.get_node(reference.NodeId)
            browse_name = reference.BrowseName.Name or ""
            node_class = reference.NodeClass

            if node_class == ua.NodeClass.Variable:
                try:
                    variant_type = await asyncio.wait_for(
                        child.read_data_type_as_variant_type(), timeout=1.5
                    )
                    data_type = getattr(variant_type, "name", str(variant_type))
                except Exception:
                    data_type = "unknown"
                print(
                    f"VARIABLE\t{child_node_id}\t{browse_name}\t{data_type}"
                )
            elif node_class in (ua.NodeClass.Object, ua.NodeClass.View):
                await visit(child, level + 1)

    await visit(objects)


async def main() -> None:
    args = parse_args()
    if not args.url:
        raise SystemExit(
            "OPC endpoint is required: use --url or set OPC_ENDPOINT"
        )

    client = Client(url=args.url, timeout=10)
    if args.username:
        client.set_user(args.username)
        client.set_password(args.password or "")

    print(f"Connecting to {args.url} ...")
    async with client:
        await browse_variables(client)


if __name__ == "__main__":
    asyncio.run(main())
