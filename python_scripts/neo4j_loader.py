import json
import os
from neo4j import GraphDatabase, TRUST_SYSTEM_CA_SIGNED_CERTIFICATES, basic_auth
from dotenv import load_dotenv
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load environment variables from .env file
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")

if not NEO4J_PASSWORD:
    logging.error("NEO4J_PASSWORD environment variable not set.")
    exit(1)

# Mapping from parsed type to Neo4j label.
# We'll use a primary 'AngularEntity' label and a specific type label.
ENTITY_TYPE_TO_LABEL_MAP = {
    "File": ["AngularEntity", "File"],
    "Component": ["AngularEntity", "Component"],
    "Service": ["AngularEntity", "Service"],
    "Module": ["AngularEntity", "Module"],
    "Pipe": ["AngularEntity", "Pipe"],
    "Directive": ["AngularEntity", "Directive"],
    "Interface": ["AngularEntity", "Interface"],
    "Class": ["AngularEntity", "Class"], # Generic class if not more specific
    "Unknown": ["AngularEntity", "UnknownEntity"],
}

UNRESOLVED_PREFIXES = ("Unresolved:", "Ambiguous:", "External:")

def get_node_labels(node_type_str: str) -> list[str]:
    """Gets the appropriate Neo4j labels for a node type."""
    return ENTITY_TYPE_TO_LABEL_MAP.get(node_type_str, ["AngularEntity", "UnknownEntity"])

def create_constraints(driver):
    """Creates unique constraints for entity IDs to ensure data integrity and performance."""
    with driver.session() as session:
        try:
            # A more generic constraint for all AngularEntity nodes by their unique 'id'
            session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (n:AngularEntity) REQUIRE n.id IS UNIQUE")
            logging.info("Ensured AngularEntity.id uniqueness constraint.")

            # Constraints for specific entity types can also be useful if you query them often by id
            # For example:
            # session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (c:Component) REQUIRE c.id IS UNIQUE")
            # session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (s:Service) REQUIRE s.id IS UNIQUE")
            # ... and so on for other types.
            # However, the generic AngularEntity constraint on 'id' covers uniqueness across all these.
        except Exception as e:
            logging.error(f"Error creating constraints: {e}")


def clear_database(driver):
    """Clears all nodes and relationships from the database."""
    logging.info("Clearing existing database...")
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
    logging.info("Database cleared.")


def load_data_to_neo4j(driver, parsed_data_file: str):
    """Loads parsed Angular data into Neo4j."""
    try:
        with open(parsed_data_file, 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        logging.error(f"Parsed data file not found: {parsed_data_file}")
        return
    except json.JSONDecodeError:
        logging.error(f"Error decoding JSON from {parsed_data_file}")
        return

    nodes_data = data.get("nodes", [])
    if not nodes_data:
        logging.warning("No nodes found in the parsed data.")
        return

    with driver.session() as session:
        logging.info(f"Starting to load {len(nodes_data)} nodes into Neo4j.")
        # --- First Pass: Create all nodes ---
        node_creation_count = 0
        for node_info in nodes_data:
            node_id = node_info.get("id")
            if not node_id:
                logging.warning(f"Skipping node due to missing ID: {node_info.get('name')}")
                continue

            node_type_str = node_info.get("type", "Unknown")
            labels = get_node_labels(node_type_str)
            
            properties = {
                "id": node_id,
                "name": node_info.get("name", "Unknown"),
                "filePath": node_info.get("filePath", ""),
                "type": node_type_str # Store original type string as a property
            }
            # Add any additional properties from the parser
            if "properties" in node_info and isinstance(node_info["properties"], dict):
                for key, value in node_info["properties"].items():
                    # Sanitize property keys if necessary (Neo4j doesn't like dots in keys)
                    properties[key.replace('.', '_')] = value
            
            # Cypher query to merge node
            # Using JOIN for labels: apoc.create.addLabels(n, $labels) might be better with APOC
            labels_cypher = ":".join(labels)
            query = f"""
            MERGE (n:AngularEntity {{id: $id}})
            ON CREATE SET n += $props, n:{labels_cypher}
            ON MATCH SET n += $props, n:{labels_cypher} 
            RETURN n.id
            """
            # For ON MATCH, ensure labels are reapplied if they somehow got removed or changed
            # Simpler ON MATCH: ON MATCH SET n += $props (if labels are not expected to change)

            try:
                session.run(query, id=node_id, props=properties)
                node_creation_count += 1
            except Exception as e:
                logging.error(f"Error creating node {node_id}: {e}")
        logging.info(f"Successfully created/merged {node_creation_count} nodes.")


        # --- Second Pass: Create relationships ---
        # Also create 'DEFINED_IN' relationships for entities to their files
        relationship_creation_count = 0
        defined_in_rel_count = 0

        for node_info in nodes_data:
            source_id = node_info.get("id")
            if not source_id:
                continue

            # Create DEFINED_IN relationship if it's not a File node itself
            if node_info.get("type") != "File" and "filePath" in node_info:
                file_id = f"File:{node_info['filePath']}"
                # Ensure the File node exists (it should have been created in the first pass)
                # If not, this relationship won't be created, which is okay.
                defin_query = """
                MATCH (source:AngularEntity {id: $source_id})
                MATCH (targetFile:File {id: $file_id})
                MERGE (source)-[:DEFINED_IN]->(targetFile)
                """
                try:
                    session.run(defin_query, source_id=source_id, file_id=file_id)
                    defined_in_rel_count +=1
                except Exception as e:
                    logging.error(f"Error creating DEFINED_IN relationship for {source_id} to {file_id}: {e}")


            # Process explicitly parsed relationships
            for rel_info in node_info.get("relationships", []):
                target_id = rel_info.get("targetId")
                rel_type = rel_info.get("type")
                rel_props = rel_info.get("properties", {})

                if not target_id or not rel_type:
                    logging.warning(f"Skipping relationship from {source_id} due to missing targetId or type.")
                    continue

                # Handle unresolved/external targets by creating/merging placeholder nodes
                target_is_special = False
                special_label = "ExternalOrUnresolved"
                target_node_label_for_match = "AngularEntity" # Default match

                for prefix in UNRESOLVED_PREFIXES:
                    if target_id.startswith(prefix):
                        actual_name = target_id[len(prefix):]
                        # Create/Merge a special node for these
                        session.run(
                            f"""MERGE (t:{special_label} {{name: $name, originalId: $originalId}})
                               ON CREATE SET t.status = $prefix""",
                            name=actual_name, originalId=target_id, prefix=prefix.strip(":")
                        )
                        target_node_label_for_match = special_label # Match this specific label
                        target_is_special = True
                        break
                
                # Build relationship query
                # Note: Relationship properties cannot have dots. Sanitize if necessary.
                sanitized_rel_props = {k.replace('.', '_'): v for k, v in rel_props.items()}

                if target_is_special:
                    # Target is one of the special prefixed nodes (External, Unresolved, Ambiguous)
                    # We need to match it by its 'originalId' if we stored it that way, or by 'name'
                    # Assuming we use 'originalId' for ExternalOrUnresolved nodes
                    rel_query = f"""
                    MATCH (source:AngularEntity {{id: $source_id}})
                    MATCH (target:{target_node_label_for_match} {{originalId: $target_id}})
                    MERGE (source)-[r:{rel_type}]->(target)
                    ON CREATE SET r = $props
                    ON MATCH SET r = $props 
                    """
                else:
                    # Target is a regular AngularEntity
                    rel_query = f"""
                    MATCH (source:AngularEntity {{id: $source_id}})
                    MATCH (target:AngularEntity {{id: $target_id}}) 
                    MERGE (source)-[r:{rel_type}]->(target)
                    ON CREATE SET r = $props
                    ON MATCH SET r = $props
                    """
                try:
                    session.run(rel_query, source_id=source_id, target_id=target_id, props=sanitized_rel_props)
                    relationship_creation_count += 1
                except Exception as e:
                    logging.error(f"Error creating relationship {source_id} -[{rel_type}]-> {target_id}: {e}")
        
        logging.info(f"Successfully created/merged {relationship_creation_count} explicit relationships.")
        logging.info(f"Successfully created/merged {defined_in_rel_count} DEFINED_IN relationships.")

def main(parsed_data_path: str, do_clear_db: bool = False):
    """Main function to connect to Neo4j and load data."""
    try:
        # Neo4j driver now uses an encrypted connection by default.
        # If your Neo4j instance doesn't have SSL configured (e.g., default local Docker setup),
        # you might need to disable encryption or configure trust.
        # For local, unencrypted: driver = GraphDatabase.driver(NEO4J_URI, auth=basic_auth(NEO4J_USER, NEO4J_PASSWORD), encrypted=False)
        # For Aura or SSL-enabled: driver = GraphDatabase.driver(NEO4J_URI, auth=basic_auth(NEO4J_USER, NEO4J_PASSWORD))
        # If using self-signed certs, you might need: trust=TRUST_SYSTEM_CA_SIGNED_CERTIFICATES or trust specific certs.
        # For simplicity, let's assume a typical local setup might not have encryption by default:
        driver = GraphDatabase.driver(NEO4J_URI, auth=basic_auth(NEO4J_USER, NEO4J_PASSWORD), encrypted=False) # Change encrypted=True for Aura/SSL
        driver.verify_connectivity()
        logging.info(f"Successfully connected to Neo4j at {NEO4J_URI}")

        create_constraints(driver)

        if do_clear_db:
            clear_database(driver)

        load_data_to_neo4j(driver, parsed_data_path)

    except Exception as e:
        logging.error(f"Failed to connect to Neo4j or process data: {e}")
    finally:
        if 'driver' in locals() and driver:
            driver.close()
            logging.info("Neo4j connection closed.")

if __name__ == "__main__":
    # Path to the JSON file generated by the TypeScript parser
    DEFAULT_PARSED_DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "output", "parsed_angular_data.json")
    
    import argparse
    parser = argparse.ArgumentParser(description="Load parsed Angular AST data into Neo4j.")
    parser.add_argument(
        "--file",
        type=str,
        default=DEFAULT_PARSED_DATA_FILE,
        help="Path to the parsed_angular_data.json file."
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear the Neo4j database before loading new data."
    )
    args = parser.parse_args()

    main(args.file, args.clear)