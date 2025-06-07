# Angular AST to Neo4j Pipeline

This project parses an Angular application into its Abstract Syntax Tree (AST), extracts structural and dependency information, and stores it in a Neo4j graph database for further querying and analysis using Python or LLMs (e.g., via [Ollama](https://ollama.com/)).

---

## Setup Instructions

### 1. Clone the Angular Example Project

Clone the Angular RealWorld example app into the root of your current working directory:

```bash
git clone https://github.com/gothinkster/angular-realworld-example-app
```

---

### 2. Install and Run the Angular AST Parser

Navigate to the `angular-ast-parser` directory and install dependencies:

```bash
cd angular-ast-parser
npm install
```

Then build and run the parser:

```bash
npm run build && node dist/parser.js ../angular-realworld-example-app > ../output/parsed_angular_data.json
```

This will generate a `parsed_angular_data.json` file inside the `output` folder.

---

### 3. Set Up the Python Environment

I recommended to use a virtual environment (using `venv` instead of `conda` to keep things lightweight):

```bash
cd python_scripts
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
cd ..
```

---

## Load Parsed Data into Neo4j

### To load data without clearing the existing database:

```bash
python python_scripts/neo4j_loader.py
```

### To clear the database before loading:

```bash
python python_scripts/neo4j_loader.py --clearing
```

### To load a custom JSON file:

```bash
python python_scripts/neo4j_loader.py --file path/to/your/data.json
```

---

## Querying the Graph

Open [Neo4j Browser](http://localhost:7474) and try sample Cypher queries:

```cypher
MATCH (n) RETURN n LIMIT 25;

MATCH (c:Component)-[:INJECTS]->(s:Service) RETURN c, s;

MATCH p=(m:Module)-[:DECLARES]->(c:Component) RETURN p;
```

---

## Analysis

Once your data is loaded, you can proceed to analyze or visualize it using `main.ipynb`.

Make sure:

* Neo4j is installed, running, and accessible.
* [Ollama](https://ollama.com/) is installed and the appropriate model is downloaded.

---

## Prerequisites

* Node.js and npm
* Python 3.8+
* Neo4j Desktop or Neo4j Server
* [Ollama](https://ollama.com/) (for optional LLM-based analysis)
