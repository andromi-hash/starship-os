# Knowledge Store

Persistent knowledge management for the starship OS — stores schematics, manuals, logs, and lessons learned.

## Capabilities

- **Document Indexing**: Index and search technical documentation
- **Log Archive**: Store and retrieve system logs with context
- **Lessons Learned**: Record solutions to problems for future reference
- **Schematic Storage**: Maintain engineering diagrams and system architecture docs
- **Cross-Reference**: Link related information across domains

## Usage

### Store Information
Save important information with tags for later retrieval.

### Search Knowledge
Search the knowledge store for relevant information.

### Record Solution
Save a problem and its solution as a lessons-learned entry.

## Implementation

Uses Hermes Agent's built-in FTS5 session search and file operations for persistent storage. Knowledge is stored as structured markdown files in the knowledge store directory.

## Dependencies

- File operations toolset
- Hermes Agent FTS5 session search
- Optional: vector database integration for semantic search
