import * as ts from 'typescript';
import * as fs from 'fs';
import * as path from 'path';

interface ParsedNode {
    id: string; // e.g., 'File:/path/to/file.ts' or 'Component:AppComponent:/path/to/component.ts'
    type: 'File' | 'Component' | 'Service' | 'Module' | 'Pipe' | 'Directive' | 'Interface' | 'Class' | 'Unknown';
    name: string;
    filePath: string;
    properties?: Record<string, any>;
    relationships: Relationship[];
}

interface Relationship {
    type:
    | 'IMPORTS'          // File to File/External
    | 'DECLARES'         // Module to Component/Pipe/Directive
    | 'PROVIDES'         // Module to Service (or other provider)
    | 'IMPORTS_MODULE'   // Module to Module
    | 'EXPORTS_MODULE'   // Module to Module/Component/Pipe/Directive (things a module exports)
    | 'BOOTSTRAPS'       // Module to Component (root component)
    | 'INJECTS'          // Component/Service to Service
    | 'DEFINED_IN'       // Entity to File (This one is added by the neo4j_loader.py, not directly by parser)
    | 'IMPLEMENTS'       // Class to Interface
    | 'USES_PIPE'        // Placeholder for template parsing
    | 'USES_DIRECTIVE';  // Placeholder for template parsing
    targetId: string;
    properties?: Record<string, any>;
}

interface Output {
    nodes: ParsedNode[];
}

function getDecoratorName(decorator: ts.Decorator): string | undefined {
    const expression = decorator.expression;
    if (ts.isCallExpression(expression)) {
        return expression.expression.getText();
    } else if (ts.isIdentifier(expression)) {
        return expression.getText();
    }
    return undefined;
}

function getDecoratorArguments(decorator: ts.Decorator): ts.NodeArray<ts.Expression> | undefined {
    if (ts.isCallExpression(decorator.expression)) {
        return decorator.expression.arguments;
    }
    return undefined;
}

function parseMetadata(objLiteral: ts.ObjectLiteralExpression, sourceFile: ts.SourceFile): Record<string, any> {
    const metadata: Record<string, any> = {};
    objLiteral.properties.forEach(prop => {
        if (ts.isPropertyAssignment(prop) && ts.isIdentifier(prop.name)) {
            const key = prop.name.text;
            const valueNode = prop.initializer;
            if (ts.isStringLiteral(valueNode) || ts.isNumericLiteral(valueNode)) { // Check String and Numeric Literals first
                metadata[key] = valueNode.text;
            } else if (valueNode.kind === ts.SyntaxKind.TrueKeyword) { // Handle TrueKeyword
                metadata[key] = true;
            } else if (valueNode.kind === ts.SyntaxKind.FalseKeyword) { // Handle FalseKeyword
                metadata[key] = false;
            } else if (ts.isArrayLiteralExpression(valueNode)) {
                metadata[key] = valueNode.elements.map(el => el.getText(sourceFile)).filter(text => text.trim() !== '');
            } else if (ts.isIdentifier(valueNode) || ts.isPropertyAccessExpression(valueNode)) {
                metadata[key] = valueNode.getText(sourceFile);
            } else {
                // For more complex structures, you might need deeper parsing or just store as text
                metadata[key] = `[Complex Value: ${valueNode.kind}]`;
            }
        }
    });
    return metadata;
}

function resolveImportPath(importPath: string, currentFilePath: string, projectBasePath: string, tsconfigPaths?: Record<string, string[]>): string {
    if (importPath.startsWith('.')) { // Relative import
        return path.resolve(path.dirname(currentFilePath), importPath);
    }
    // Try resolving with tsconfig paths (simplified)
    if (tsconfigPaths) {
        for (const [key, paths] of Object.entries(tsconfigPaths)) {
            const alias = key.replace(/\*$/, '');
            if (importPath.startsWith(alias)) {
                for (const p of paths) {
                    const resolvedPath = path.resolve(projectBasePath, p.replace(/\*$/, ''), importPath.substring(alias.length));
                    // This is a simplification; actual module resolution is more complex
                    // We're hoping to find a .ts file or a directory with index.ts
                    if (fs.existsSync(resolvedPath + '.ts')) return resolvedPath + '.ts';
                    if (fs.existsSync(path.join(resolvedPath, 'index.ts'))) return path.join(resolvedPath, 'index.ts');
                    // Fallback for paths that might already be fully resolved by the alias
                    if (fs.existsSync(resolvedPath)) return resolvedPath; // If it's a file directly
                }
            }
        }
    }
    // If it's a library or unresolvable local path, return as is (or mark as external)
    // For simplicity, we'll assume local paths if not found.
    // A more robust solution would check node_modules or specific library mappings.
    const possibleLocalPath = path.resolve(projectBasePath, importPath + '.ts');
    if (fs.existsSync(possibleLocalPath)) {
        return possibleLocalPath;
    }
    return importPath; // Could be a node_module or unresolvable
}


function parseAngularCode(projectPath: string, tsconfigPath?: string): Output {
    const nodes: ParsedNode[] = [];
    const fileMap = new Map<string, ParsedNode>(); // To quickly find file nodes by path

    const configFileName = ts.findConfigFile(
        projectPath,
        ts.sys.fileExists,
        tsconfigPath || 'tsconfig.json' // Allow specifying tsconfig, e.g., tsconfig.app.json
    );

    if (!configFileName) {
        throw new Error("Could not find a valid 'tsconfig.json'.");
    }
    const configFile = ts.readConfigFile(configFileName, ts.sys.readFile);
    const compilerOptions = ts.parseJsonConfigFileContent(
        configFile.config,
        ts.sys,
        path.dirname(configFileName)
    );

    const program = ts.createProgram(compilerOptions.fileNames, compilerOptions.options);
    const allSourceFiles = program.getSourceFiles();
    const typeChecker = program.getTypeChecker();

    const tsconfigPaths = compilerOptions.options.paths;

    for (const sourceFile of allSourceFiles) {
        // Skip declaration files and files outside the project path (e.g. node_modules)
        if (sourceFile.isDeclarationFile || !sourceFile.fileName.startsWith(path.resolve(projectPath))) {
            continue;
        }

        const relativeFilePath = path.relative(projectPath, sourceFile.fileName).replace(/\\/g, '/');
        const fileId = `File:${relativeFilePath}`;

        if (!fileMap.has(fileId)) {
            const fileNode: ParsedNode = {
                id: fileId,
                type: 'File',
                name: path.basename(sourceFile.fileName),
                filePath: relativeFilePath,
                relationships: [],
            };
            nodes.push(fileNode);
            fileMap.set(fileId, fileNode);
        }
        const currentFileNode = fileMap.get(fileId)!;


        ts.forEachChild(sourceFile, (node) => {
            if (ts.isImportDeclaration(node)) {
                if (node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
                    const importPathRaw = node.moduleSpecifier.text;
                    const resolvedImportPath = resolveImportPath(importPathRaw, sourceFile.fileName, projectPath, tsconfigPaths);
                    const targetFileId = `File:${path.relative(projectPath, resolvedImportPath).replace(/\\/g, '/')}`;

                    // Ensure target file node exists (even if empty, if it's external or unparsed)
                    if (!fileMap.has(targetFileId) && (resolvedImportPath.includes('.ts') || resolvedImportPath.includes('.js'))) { // Only add if it looks like a code file
                        const targetFileNode: ParsedNode = {
                            id: targetFileId,
                            type: 'File',
                            name: path.basename(resolvedImportPath),
                            filePath: path.relative(projectPath, resolvedImportPath).replace(/\\/g, '/'),
                            relationships: []
                        };
                        nodes.push(targetFileNode);
                        fileMap.set(targetFileId, targetFileNode);
                    }

                    if (fileMap.has(targetFileId) || !resolvedImportPath.startsWith('.')) { // Add relationship if target file exists or it's a library import
                        currentFileNode.relationships.push({
                            type: 'IMPORTS',
                            targetId: fileMap.has(targetFileId) ? targetFileId : `External:${importPathRaw}`, // Link to file or mark as external
                            properties: { from: importPathRaw }
                        });
                    }
                }
            } else if (ts.isClassDeclaration(node) && node.name) {
                const className = node.name.text;
                const classId = `${className}:${relativeFilePath}`; // Base ID for any class-based entity

                let entityType: ParsedNode['type'] = 'Class';
                let entityName = className;
                let entityId = classId;
                const entityProperties: Record<string, any> = {};
                const entityRelationships: Relationship[] = [];

                const decorators = ts.getDecorators(node);
                if (decorators) {
                    for (const decorator of decorators) {
                        const decoratorName = getDecoratorName(decorator);
                        const decoratorArgs = getDecoratorArguments(decorator);

                        if (decoratorName === 'Component' && decoratorArgs && decoratorArgs[0] && ts.isObjectLiteralExpression(decoratorArgs[0])) {
                            entityType = 'Component';
                            entityId = `Component:${className}:${relativeFilePath}`;
                            const metadata = parseMetadata(decoratorArgs[0], sourceFile);
                            if (metadata.selector) entityProperties['selector'] = metadata.selector;
                            if (metadata.templateUrl) entityProperties['templateUrl'] = metadata.templateUrl;
                            if (metadata.styleUrls) entityProperties['styleUrls'] = metadata.styleUrls;
                            // TODO: Parse standalone, imports for standalone components
                        } else if (decoratorName === 'Injectable' && decoratorArgs && decoratorArgs[0] && ts.isObjectLiteralExpression(decoratorArgs[0])) {
                            entityType = 'Service';
                            entityId = `Service:${className}:${relativeFilePath}`;
                            const metadata = parseMetadata(decoratorArgs[0], sourceFile);
                            if (metadata.providedIn) entityProperties['providedIn'] = metadata.providedIn;
                        } else if (decoratorName === 'Injectable' && !decoratorArgs) { // @Injectable()
                            entityType = 'Service';
                            entityId = `Service:${className}:${relativeFilePath}`;
                        }
                        else if (decoratorName === 'NgModule' && decoratorArgs && decoratorArgs[0] && ts.isObjectLiteralExpression(decoratorArgs[0])) {
                            entityType = 'Module';
                            entityId = `Module:${className}:${relativeFilePath}`;
                            const metadata = parseMetadata(decoratorArgs[0], sourceFile);
                            // Process 'declarations': Components, Directives, Pipes
                            (metadata.declarations as string[] || []).forEach(declarationName => {
                                // The declared item could be a Component, Directive, or Pipe.
                                // We create a placeholder targetId. The resolution pass will try to find it.
                                // We don't know its exact type yet, so 'Unknown' or try to infer from common suffixes.
                                let targetTypePrefix = 'Unknown';
                                if (declarationName.endsWith('Component')) targetTypePrefix = 'Component';
                                else if (declarationName.endsWith('Directive')) targetTypePrefix = 'Directive';
                                else if (declarationName.endsWith('Pipe')) targetTypePrefix = 'Pipe';

                                entityRelationships.push({
                                    type: 'DECLARES',
                                    // Target ID: Assume same file for now, resolution pass will correct if imported
                                    targetId: `${targetTypePrefix}:${declarationName}:${relativeFilePath}`
                                });
                            });

                            // Process 'imports': Other NgModules
                            (metadata.imports as string[] || []).forEach(importName => {
                                entityRelationships.push({
                                    type: 'IMPORTS_MODULE',
                                    // Target ID: Assume it's a Module. Resolution pass will find its file.
                                    targetId: `Module:${importName}:UNKNOWN_PATH` // Needs full resolution
                                });
                            });

                            // Process 'providers': Services or other injectables
                            (metadata.providers as string[] || []).forEach(providerName => {
                                // Providers can be complex (useClass, useValue, useFactory, InjectionToken).
                                // For simplicity, we assume simple class providers (Services).
                                // A more robust parser would analyze the provider object structure.
                                entityRelationships.push({
                                    type: 'PROVIDES',
                                    // Target ID: Assume Service. Resolution pass will find its file.
                                    targetId: `Service:${providerName}:UNKNOWN_PATH` // Needs full resolution
                                });
                            });

                            // Process 'exports': Modules, Components, Directives, Pipes
                            (metadata.exports as string[] || []).forEach(exportName => {
                                // Similar to declarations, the exported item can be various types.
                                // We'll create a generic 'Unknown' target; resolution is key.
                                // It could be exporting a re-exported Module, or a Component/Directive/Pipe from its own declarations.
                                entityRelationships.push({
                                    type: 'EXPORTS_MODULE', // Using EXPORTS_MODULE broadly for anything exported
                                    // Consider a more specific 'EXPORTS_ENTITY' if needed
                                    // Target ID: Could be Module, Component, etc. Resolution pass is crucial.
                                    targetId: `UnknownExport:${exportName}:UNKNOWN_PATH` // Needs full resolution
                                });
                            });

                            // Process 'bootstrap': Root Components for this module
                            (metadata.bootstrap as string[] || []).forEach(bootstrapComponentName => {
                                entityRelationships.push({
                                    type: 'BOOTSTRAPS',
                                    targetId: `Component:${bootstrapComponentName}:UNKNOWN_PATH` // Needs full resolution
                                });
                            });

                            if (metadata.id) entityProperties['moduleId'] = metadata.id;

                        } else if (decoratorName === 'Pipe' && decoratorArgs && decoratorArgs[0] && ts.isObjectLiteralExpression(decoratorArgs[0])) {
                            entityType = 'Pipe';
                            entityId = `Pipe:${className}:${relativeFilePath}`;
                            const metadata = parseMetadata(decoratorArgs[0], sourceFile);
                            if (metadata.name) entityProperties['pipeName'] = metadata.name;
                        } else if (decoratorName === 'Directive' && decoratorArgs && decoratorArgs[0] && ts.isObjectLiteralExpression(decoratorArgs[0])) {
                            entityType = 'Directive';
                            entityId = `Directive:${className}:${relativeFilePath}`;
                            const metadata = parseMetadata(decoratorArgs[0], sourceFile);
                            if (metadata.selector) entityProperties['selector'] = metadata.selector;
                        }
                    }
                }

                // Dependency Injection in Constructor
                if (node.members) {
                    for (const member of node.members) {
                        if (ts.isConstructorDeclaration(member) && member.parameters) {
                            member.parameters.forEach(param => {
                                if (param.type && param.name && ts.isIdentifier(param.name)) {
                                    const paramName = param.name.text;
                                    const paramTypeName = param.type!.getText(sourceFile);
                                    // This needs proper resolution to the actual Service ID
                                    // For now, we assume the type name is the Service name and it's in some file.
                                    // A more robust system would use the typechecker to resolve the symbol.
                                    entityRelationships.push({
                                        type: 'INJECTS',
                                        targetId: `Service:${paramTypeName}:UNKNOWN_PATH`, // Placeholder, needs resolution
                                        properties: { parameterName: paramName }
                                    });
                                }
                            });
                        }
                    }
                }

                // Implements clauses
                if (node.heritageClauses) {
                    for (const clause of node.heritageClauses) {
                        if (clause.token === ts.SyntaxKind.ImplementsKeyword) {
                            for (const type of clause.types) {
                                const implementedInterfaceName = type.expression.getText(sourceFile);
                                // This also needs proper resolution.
                                entityRelationships.push({
                                    type: 'IMPLEMENTS',
                                    targetId: `Interface:${implementedInterfaceName}:UNKNOWN_PATH` // Placeholder
                                });
                            }
                        }
                    }
                }

                // Add the identified entity
                const existingNode = nodes.find(n => n.id === entityId);
                if (existingNode) { // Merge if already created (e.g. by DECLARES relationship placeholder)
                    existingNode.type = entityType;
                    existingNode.name = entityName;
                    existingNode.filePath = relativeFilePath;
                    existingNode.properties = { ...existingNode.properties, ...entityProperties };
                    existingNode.relationships.push(...entityRelationships);
                } else {
                    nodes.push({
                        id: entityId,
                        type: entityType,
                        name: entityName,
                        filePath: relativeFilePath,
                        properties: entityProperties,
                        relationships: entityRelationships,
                    });
                }

            } else if (ts.isInterfaceDeclaration(node) && node.name) {
                const interfaceName = node.name.text;
                const interfaceId = `Interface:${interfaceName}:${relativeFilePath}`;
                const existingNode = nodes.find(n => n.id === interfaceId);
                if (!existingNode) {
                    nodes.push({
                        id: interfaceId,
                        type: 'Interface',
                        name: interfaceName,
                        filePath: relativeFilePath,
                        relationships: []
                    });
                }
            }
            // TODO: Add more handlers for other Angular constructs (Directives, Pipes, Guards, Resolvers, etc.)
            // TODO: Parse template files (.html) for component relationships (e.g. <app-child>)
            // TODO: Parse SCSS/CSS files for style relationships
        });
    }

    // Second pass to resolve relationship targetIds (simple placeholder for now)
    // A full resolution would involve looking up symbols in imported files.
    // This is a complex task and often requires semantic understanding from the TypeChecker.
    for (const node of nodes) {
        for (const rel of node.relationships) {
            const targetId = rel.targetId;

            if (
                targetId.startsWith('Unknown:') ||
                targetId.endsWith(':UNKNOWN_PATH') ||
                targetId.includes(':undefined:') ||
                targetId.startsWith('Module:') ||
                targetId.startsWith('Service:') ||
                targetId.startsWith('Component:') ||
                targetId.startsWith('UnknownExport:')
            ) {
                const parts = targetId.split(':');
                let targetTypeHint: string | undefined;
                let targetName: string;
                let targetFilePathHint: string | undefined;

                if (targetId.startsWith('UnknownExport:')) {
                    targetTypeHint = undefined;
                    targetName = parts[1];
                } else if (parts.length === 3 && parts[2] === 'UNKNOWN_PATH') {
                    targetTypeHint = parts[0];
                    targetName = parts[1];
                } else if (targetId.startsWith('Unknown:')) {
                    targetTypeHint = parts[0] === 'Unknown' ? undefined : parts[0];
                    targetName = parts[1];
                    targetFilePathHint = parts.length > 2 ? parts.slice(2).join(':') : node.filePath;
                } else {
                    targetName = parts[1] || parts[0];
                }

                // Attempt to find a matching node by name across all files
                const potentialTargets = nodes.filter(n =>
                    n.name === targetName &&
                    (targetTypeHint === undefined || n.id.startsWith(`${targetTypeHint}:`))
                );

                if (potentialTargets.length === 1) {
                    rel.targetId = potentialTargets[0].id;
                } else if (potentialTargets.length > 1) {
                    rel.targetId = `Ambiguous:${targetName}`;
                    // Optionally log ambiguity
                    // console.warn(`Ambiguous resolution for ${targetId}. Candidates: ${potentialTargets.map(t => t.id).join(', ')}`);
                } else {
                    rel.targetId = `Unresolved:${targetName}`;
                    // Optionally log failure to resolve
                    // console.warn(`Could not resolve targetId: ${targetId}`);
                }
            }
        }
    }



    return { nodes };
}

// --- Main Execution ---
if (require.main === module) {
    const args = process.argv.slice(2);
    if (args.length < 1) {
        console.error("Usage: node parser.js <pathToAngularProject> [pathToTsConfig]");
        process.exit(1);
    }
    const projectPath = path.resolve(args[0]);
    const tsConfig = args[1] ? path.resolve(args[1]) : path.join(projectPath, 'tsconfig.app.json'); // Default to tsconfig.app.json

    if (!fs.existsSync(projectPath)) {
        console.error(`Project path does not exist: ${projectPath}`);
        process.exit(1);
    }
    if (!fs.existsSync(tsConfig)) {
        console.warn(`Warning: tsconfig.app.json not found at ${tsConfig}. Trying tsconfig.json in project root.`);
        const rootTsConfig = path.join(projectPath, 'tsconfig.json');
        if (!fs.existsSync(rootTsConfig)) {
            console.error(`Error: Neither ${tsConfig} nor ${rootTsConfig} found.`);
            process.exit(1);
        }
        // Fallback to root tsconfig.json if tsconfig.app.json is not present
        const parsedData = parseAngularCode(projectPath, rootTsConfig);
        console.log(JSON.stringify(parsedData, null, 2));
    } else {
        const parsedData = parseAngularCode(projectPath, tsConfig);
        console.log(JSON.stringify(parsedData, null, 2));
    }
}