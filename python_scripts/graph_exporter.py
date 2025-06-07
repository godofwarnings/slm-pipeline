import json
import os
from neo4j import GraphDatabase, basic_auth
from dotenv import load_dotenv
import logging
from jsonschema import Draft7Validator  # For generating JSON schemas

# Configure logging
logging.basicConfig(
    level=logging.INFO,  # Set to logging.DEBUG for very verbose output
    format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s",
)

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")

# Resolve OUTPUT_DIR and log its absolute path
try:
    OUTPUT_DIR = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "output")
    )
    logging.info(f"Output directory configured to: {OUTPUT_DIR}")
except Exception as e:
    logging.error(f"Error resolving OUTPUT_DIR path: {e}", exc_info=True)
    OUTPUT_DIR = "./output"  # Fallback, though this might not be ideal
    logging.warning(f"Falling back OUTPUT_DIR to: {os.path.abspath(OUTPUT_DIR)}")


if not NEO4J_PASSWORD:
    logging.error(
        "NEO4J_PASSWORD environment variable not set. Please check your .env file."
    )
    exit(1)

# Ensure output directory exists
try:
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        logging.info(f"Created output directory: {OUTPUT_DIR}")
    else:
        logging.info(f"Output directory {OUTPUT_DIR} already exists.")
except Exception as e:
    logging.error(
        f"Failed to create or access output directory {OUTPUT_DIR}: {e}", exc_info=True
    )
    exit(1)  # Exit if we can't ensure the output directory


def get_neo4j_driver():
    logging.info(f"Attempting to connect to Neo4j: URI={NEO4J_URI}, User={NEO4J_USER}")
    try:
        driver = GraphDatabase.driver(
            NEO4J_URI, auth=basic_auth(NEO4J_USER, NEO4J_PASSWORD), encrypted=False
        )  # Adjust encrypted for your setup
        driver.verify_connectivity()
        logging.info(f"Successfully connected to Neo4j at {NEO4J_URI} for export.")
        return driver
    except Exception as e:
        logging.error(f"Failed to connect to Neo4j for export: {e}", exc_info=True)
        raise


def get_type(value):
    """Infer JSON schema type from Python type."""
    if isinstance(value, bool):
        return "boolean"
    elif isinstance(value, int):
        return "integer"
    elif isinstance(value, float):
        return "number"
    elif isinstance(value, str):
        return "string"
    elif isinstance(value, list):
        return "array"
    elif isinstance(value, dict):
        return "object"
    return "null"


def generate_json_schema(instance_data, title="Schema", description=""):
    """Generates a basic JSON schema from an instance object or list of objects."""
    logging.debug(f"Generating JSON schema for title: {title}")
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": title,
        "description": description,
    }

    if isinstance(instance_data, list):
        schema["type"] = "array"
        if instance_data:
            first_item = instance_data[0]
            logging.debug(f"Schema for list, first item type: {type(first_item)}")
            # Generate schema for the first item to define "items"
            item_schema_full = generate_json_schema(first_item, title=f"{title} Item")
            # Determine if the item itself is a complex object or a primitive
            if (
                item_schema_full.get("type") == "object"
                and "properties" in item_schema_full
            ):
                schema["items"] = {
                    "type": "object",
                    "properties": item_schema_full["properties"],
                }
                if "required" in item_schema_full:  # Carry over required if present
                    schema["items"]["required"] = item_schema_full["required"]
            else:  # Primitive type or an array/object without 'properties' directly (e.g. a root array/object)
                schema["items"] = {
                    "type": item_schema_full.get("type", get_type(first_item))
                }
        else:
            schema["items"] = {}
            logging.debug("Schema for list: empty list, so empty items schema.")
    elif isinstance(instance_data, dict):
        schema["type"] = "object"
        properties = {}
        # required = [] # Decide if all keys are required by default
        for key, value in instance_data.items():
            prop_schema_full = generate_json_schema(
                value, title=f"{title} Property: {key}"
            )
            # We just want the type or the full schema if it's complex
            if (
                prop_schema_full.get("type") == "object"
                and "properties" in prop_schema_full
            ):
                properties[key] = prop_schema_full  # Keep full nested schema
            elif (
                prop_schema_full.get("type") == "array" and "items" in prop_schema_full
            ):
                properties[key] = prop_schema_full  # Keep full array schema
            else:
                properties[key] = {
                    "type": prop_schema_full.get("type", get_type(value))
                }
            # required.append(key)
        schema["properties"] = properties
        # if required: schema["required"] = required
        logging.debug(
            f"Schema for dict: generated properties: {list(properties.keys())}"
        )
    else:
        schema["type"] = get_type(instance_data)
        logging.debug(f"Schema for primitive: type={schema['type']}")
    return schema


def export_architecture_schema(driver):
    logging.info("Starting export_architecture_schema function...")
    architecture = {"nodes": [], "relationships": []}

    with driver.session() as session:
        logging.info("Querying Neo4j for distinct node types and sample properties...")
        node_labels_query = """
        MATCH (n)
        UNWIND labels(n) AS lbl
        WITH lbl, n
        WHERE NOT lbl IN ['AngularEntity', 'ExternalOrUnresolved']
        WITH DISTINCT lbl AS nodeType, head(collect(properties(n))) AS sampleProps
        RETURN nodeType, sampleProps
        ORDER BY nodeType
        """
        try:
            node_results = session.run(node_labels_query)
            node_records = list(node_results)  # Consume results
            logging.info(
                f"Found {len(node_records)} distinct primary node types from query."
            )
            for record in node_records:
                node_type = record["nodeType"]
                sample_props = record["sampleProps"] if record["sampleProps"] else {}
                prop_definitions = {k: get_type(v) for k, v in sample_props.items()}
                architecture["nodes"].append(
                    {"type": node_type, "properties": prop_definitions}
                )
                logging.debug(
                    f"Processed node type for architecture: {node_type} with {len(prop_definitions)} properties."
                )
        except Exception as e:
            logging.error(
                f"Error querying or processing node types: {e}", exc_info=True
            )

        logging.info("Querying Neo4j for ExternalOrUnresolved node properties...")
        ext_unresolved_query = """
        MATCH (n:ExternalOrUnresolved)
        RETURN head(collect(properties(n))) as sampleProps
        LIMIT 1
        """
        try:
            ext_result = session.run(ext_unresolved_query).single()
            if ext_result and ext_result["sampleProps"]:
                sample_props = ext_result["sampleProps"]
                prop_definitions = {k: get_type(v) for k, v in sample_props.items()}
                architecture["nodes"].append(
                    {"type": "ExternalOrUnresolved", "properties": prop_definitions}
                )
                logging.info("Processed ExternalOrUnresolved type for architecture.")
            else:
                logging.info(
                    "No ExternalOrUnresolved nodes found or they have no properties."
                )
        except Exception as e:
            logging.error(
                f"Error querying or processing ExternalOrUnresolved type: {e}",
                exc_info=True,
            )

        logging.info(
            "Querying Neo4j for distinct relationship types and sample properties..."
        )
        rel_types_query = """
        MATCH ()-[r]->()
        WITH DISTINCT type(r) AS relType, head(collect(properties(r))) as sampleProps
        RETURN relType, sampleProps
        ORDER BY relType
        """
        try:
            rel_results = session.run(rel_types_query)
            rel_records = list(rel_results)  # Consume results
            logging.info(
                f"Found {len(rel_records)} distinct relationship types from query."
            )
            for record in rel_records:
                rel_type = record["relType"]
                sample_props = record["sampleProps"] if record["sampleProps"] else {}
                prop_definitions = {k: get_type(v) for k, v in sample_props.items()}
                architecture["relationships"].append(
                    {"type": rel_type, "properties": prop_definitions}
                )
                logging.debug(
                    f"Processed relationship type for architecture: {rel_type} with {len(prop_definitions)} properties."
                )
        except Exception as e:
            logging.error(
                f"Error querying or processing relationship types: {e}", exc_info=True
            )

    logging.debug(
        f"Final architecture data before writing: {json.dumps(architecture, indent=2)}"
    )

    arch_path = os.path.join(OUTPUT_DIR, "architecture.json")
    logging.info(f"Attempting to write architecture data to: {arch_path}")
    try:
        with open(arch_path, "w") as f:
            json.dump(architecture, f, indent=2)
        logging.info(f"Successfully exported graph schema to {arch_path}")
    except Exception as e:
        logging.error(f"Failed to write {arch_path}: {e}", exc_info=True)

    logging.info("Generating JSON schema for architecture.json...")
    sample_arch_for_schema = {  # Default sample if architecture is empty
        "nodes": [{"type": "ExampleNodeType", "properties": {"prop1": "string"}}],
        "relationships": [{"type": "ExampleRelType", "properties": {"relProp1": True}}],
    }
    if architecture["nodes"]:  # Use actual data if available
        sample_arch_for_schema["nodes"] = [architecture["nodes"][0]]
    if architecture["relationships"]:  # Use actual data if available
        sample_arch_for_schema["relationships"] = [architecture["relationships"][0]]

    try:
        arch_schema = generate_json_schema(
            sample_arch_for_schema,
            title="Graph Architecture Schema",
            description="Defines the structure for specifying node and relationship types and their properties.",
        )
        # Refinements for the schema structure
        if (
            "properties" in arch_schema
            and "nodes" in arch_schema["properties"]
            and "items" in arch_schema["properties"]["nodes"]
        ):
            arch_schema["properties"]["nodes"]["items"] = {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "description": "The label/type of the node.",
                    },
                    "properties": {
                        "type": "object",
                        "description": "Key-value pairs where key is property name and value is its JSON schema type (string).",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["type", "properties"],
            }
        if (
            "properties" in arch_schema
            and "relationships" in arch_schema["properties"]
            and "items" in arch_schema["properties"]["relationships"]
        ):
            arch_schema["properties"]["relationships"]["items"] = {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "description": "The type of the relationship.",
                    },
                    "properties": {
                        "type": "object",
                        "description": "Key-value pairs where key is property name and value is its JSON schema type (string).",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["type", "properties"],
            }

        arch_schema_path = os.path.join(OUTPUT_DIR, "architecture_schema.json")
        logging.info(
            f"Attempting to write architecture_schema.json to: {arch_schema_path}"
        )
        with open(arch_schema_path, "w") as f:
            json.dump(arch_schema, f, indent=2)
        logging.info(
            f"Successfully exported graph schema's JSON schema to {arch_schema_path}"
        )
    except Exception as e:
        logging.error(
            f"Failed to generate or write architecture_schema.json: {e}", exc_info=True
        )
    logging.info("Finished export_architecture_schema function.")


def export_data_model(driver):
    logging.info("Starting export_data_model function...")
    data_model = {"nodes": [], "relationships": []}
    sample_node_for_schema = None
    sample_rel_for_schema = None

    with driver.session() as session:
        logging.info("Querying Neo4j for all nodes...")
        nodes_query = """
        MATCH (n)
        WITH n, labels(n) as lbls
        RETURN
            elementId(n) AS elementId,
            CASE
                WHEN size([lbl IN lbls WHERE NOT lbl IN ['AngularEntity', 'ExternalOrUnresolved']]) > 0 THEN [lbl IN lbls WHERE NOT lbl IN ['AngularEntity', 'ExternalOrUnresolved']][0]
                WHEN 'ExternalOrUnresolved' IN lbls THEN 'ExternalOrUnresolved'
                WHEN 'AngularEntity' IN lbls THEN 'AngularEntity'
                ELSE 'UnknownNode' // Fallback if no expected labels
            END AS effectiveLabel,
            properties(n) AS props
        """
        try:
            node_results = session.run(nodes_query)
            node_records = list(node_results)
            logging.info(f"Retrieved {len(node_records)} nodes from Neo4j.")
            for record_idx, record in enumerate(node_records):
                node_data = {
                    "_id": record["elementId"],
                    "labels": (
                        [record["effectiveLabel"]]
                        if record["effectiveLabel"]
                        else ["UnknownNode"]
                    ),
                    "properties": record["props"] if record["props"] else {},
                }
                if "id" in node_data["properties"]:  # Our business ID
                    node_data["id"] = node_data["properties"]["id"]

                data_model["nodes"].append(node_data)
                if not sample_node_for_schema and node_data["properties"]:
                    sample_node_for_schema = node_data
                elif not sample_node_for_schema:  # If first node has no properties
                    sample_node_for_schema = (
                        node_data  # Still use it for _id, labels structure
                    )

                if record_idx < 5:  # Log first few nodes for debugging
                    logging.debug(
                        f"Processed node for data_model (sample): {node_data}"
                    )

        except Exception as e:
            logging.error(
                f"Error querying or processing nodes for data_model: {e}", exc_info=True
            )

        logging.info("Querying Neo4j for all relationships...")
        rels_query = """
        MATCH (n)-[r]->(m)
        RETURN
            elementId(r) AS elementId,
            elementId(n) AS sourceElementId,
            elementId(m) AS targetElementId,
            type(r) AS type,
            properties(r) AS props
        """
        try:
            rel_results = session.run(rels_query)
            rel_records = list(rel_results)
            logging.info(f"Retrieved {len(rel_records)} relationships from Neo4j.")
            for record_idx, record in enumerate(rel_records):
                rel_data = {
                    "_id": record["elementId"],
                    "_sourceId": record["sourceElementId"],
                    "_targetId": record["targetElementId"],
                    "type": record["type"],
                    "properties": record["props"] if record["props"] else {},
                }
                data_model["relationships"].append(rel_data)
                if not sample_rel_for_schema and rel_data["properties"]:
                    sample_rel_for_schema = rel_data
                elif not sample_rel_for_schema:  # If first rel has no properties
                    sample_rel_for_schema = rel_data  # Still use it

                if record_idx < 5:  # Log first few rels for debugging
                    logging.debug(
                        f"Processed relationship for data_model (sample): {rel_data}"
                    )
        except Exception as e:
            logging.error(
                f"Error querying or processing relationships for data_model: {e}",
                exc_info=True,
            )

    logging.debug(
        f"Final data_model before writing (nodes: {len(data_model['nodes'])}, rels: {len(data_model['relationships'])})."
    )

    model_path = os.path.join(OUTPUT_DIR, "data_model.json")
    logging.info(f"Attempting to write data_model.json to: {model_path}")
    try:
        with open(model_path, "w") as f:
            json.dump(data_model, f, indent=2)
        logging.info(f"Successfully exported full data model to {model_path}")
    except Exception as e:
        logging.error(f"Failed to write {model_path}: {e}", exc_info=True)

    logging.info("Generating JSON schema for data_model.json...")
    # Define default sample structures in case data_model is empty
    default_sample_node = {
        "_id": "node_el_id_default",
        "labels": ["DefaultLabel"],
        "id": "node_biz_id_default",
        "properties": {"name": "Default Node", "type": "DefaultType"},
    }
    default_sample_rel = {
        "_id": "rel_el_id_default",
        "_sourceId": "node_el_id_1",
        "_targetId": "node_el_id_2",
        "type": "DEFAULT_REL",
        "properties": {"description": "Default relationship"},
    }

    final_sample_node = (
        sample_node_for_schema if sample_node_for_schema else default_sample_node
    )
    final_sample_rel = (
        sample_rel_for_schema if sample_rel_for_schema else default_sample_rel
    )

    sample_data_for_schema = {
        "nodes": (
            [final_sample_node] if data_model["nodes"] else []
        ),  # Use actual first node if exists, else empty for schema
        "relationships": [final_sample_rel] if data_model["relationships"] else [],
    }
    # Ensure samples are not empty lists if the main data is empty, for schema generation to have something to work with
    if not sample_data_for_schema["nodes"]:
        sample_data_for_schema["nodes"] = [default_sample_node]
    if not sample_data_for_schema["relationships"]:
        sample_data_for_schema["relationships"] = [default_sample_rel]

    try:
        model_schema = generate_json_schema(
            sample_data_for_schema,
            title="Graph Data Model Instance Schema",
            description="Defines the structure for representing graph nodes and relationships.",
        )

        # Refinements for node item schema
        node_item_props = {
            "_id": {
                "type": "string",
                "description": "Internal element ID of the node.",
            },
            "labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of labels for the node.",
            },
            "properties": {
                "type": "object",
                "description": "Key-value properties of the node.",
                "additionalProperties": True,
            },
        }
        if "id" in final_sample_node:  # Check if our business 'id' is typically present
            node_item_props["id"] = {
                "type": "string",
                "description": "Primary business identifier of the node.",
            }

        # Ensure path exists in schema before assignment
        if model_schema.get("properties", {}).get("nodes", {}).get("items"):
            model_schema["properties"]["nodes"]["items"]["properties"] = node_item_props
            model_schema["properties"]["nodes"]["items"]["required"] = [
                "_id",
                "labels",
                "properties",
            ]
            if "id" in node_item_props:
                model_schema["properties"]["nodes"]["items"]["required"].append("id")
        else:  # Fallback if schema structure is not as expected
            logging.warning(
                "Node items schema structure unexpected, applying fallback."
            )
            if not model_schema.get("properties"):
                model_schema["properties"] = {}
            if not model_schema["properties"].get("nodes"):
                model_schema["properties"]["nodes"] = {"type": "array", "items": {}}
            model_schema["properties"]["nodes"]["items"] = {
                "type": "object",
                "properties": node_item_props,
                "required": ["_id", "labels", "properties"],
            }

        # Refinements for relationship item schema
        rel_item_props = {
            "_id": {
                "type": "string",
                "description": "Internal element ID of the relationship.",
            },
            "_sourceId": {
                "type": "string",
                "description": "Element ID of the source node.",
            },
            "_targetId": {
                "type": "string",
                "description": "Element ID of the target node.",
            },
            "type": {"type": "string", "description": "Type of the relationship."},
            "properties": {
                "type": "object",
                "description": "Key-value properties of the relationship.",
                "additionalProperties": True,
            },
        }
        if model_schema.get("properties", {}).get("relationships", {}).get("items"):
            model_schema["properties"]["relationships"]["items"][
                "properties"
            ] = rel_item_props
            model_schema["properties"]["relationships"]["items"]["required"] = [
                "_id",
                "_sourceId",
                "_targetId",
                "type",
                "properties",
            ]
        else:  # Fallback
            logging.warning(
                "Relationship items schema structure unexpected, applying fallback."
            )
            if not model_schema.get("properties"):
                model_schema["properties"] = {}
            if not model_schema["properties"].get("relationships"):
                model_schema["properties"]["relationships"] = {
                    "type": "array",
                    "items": {},
                }
            model_schema["properties"]["relationships"]["items"] = {
                "type": "object",
                "properties": rel_item_props,
                "required": ["_id", "_sourceId", "_targetId", "type", "properties"],
            }

        model_schema_path = os.path.join(OUTPUT_DIR, "data_model_schema.json")
        logging.info(
            f"Attempting to write data_model_schema.json to: {model_schema_path}"
        )
        with open(model_schema_path, "w") as f:
            json.dump(model_schema, f, indent=2)
        logging.info(
            f"Successfully exported data model's JSON schema to {model_schema_path}"
        )
    except Exception as e:
        logging.error(
            f"Failed to generate or write data_model_schema.json: {e}", exc_info=True
        )
    logging.info("Finished export_data_model function.")


def main():
    logging.info("graph_exporter.py script started.")
    driver = None
    # Test file write to ensure directory is writable
    test_file_path = os.path.join(OUTPUT_DIR, "startup_test_output.txt")
    try:
        with open(test_file_path, "w") as f:
            f.write(
                f"Test content from graph_exporter at {logging.getLogger().handlers[0].formatter._style._fmt % {'asctime': logging.Formatter().formatTime(logging.LogRecord('', '', '', '', '', '', '', None)), 'levelname': 'INFO', 'filename': 'graph_exporter.py', 'lineno': 0, 'message': 'startup test'}}"
            )
        logging.info(f"Successfully wrote startup test file to {test_file_path}")
        os.remove(test_file_path)  # Clean up test file
        logging.info(f"Removed startup test file {test_file_path}")
    except Exception as e:
        logging.error(
            f"Failed to write startup test file {test_file_path}. Halting execution. Check permissions and path. Error: {e}",
            exc_info=True,
        )
        return  # Halt if we can't even write a test file

    try:
        driver = get_neo4j_driver()
        export_architecture_schema(driver)
        export_data_model(driver)
        logging.info("All export functions completed.")
    except Exception as e:
        # This will catch exceptions from get_neo4j_driver if it fails before returning
        logging.error(
            f"A critical error occurred in the main execution block: {e}", exc_info=True
        )
    finally:
        if driver:
            driver.close()
            logging.info("Neo4j connection closed after export.")
        else:
            logging.warning("Neo4j driver was not initialized or connection failed.")
    logging.info("graph_exporter.py script finished.")


if __name__ == "__main__":
    main()
