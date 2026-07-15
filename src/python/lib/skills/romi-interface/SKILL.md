# Romi Interface

User-facing natural language interaction patterns for the Starship OS.

## Capabilities

- **Natural Language Understanding**: Interpret user requests and intents
- **Task Explanation**: Break down complex operations into understandable steps
- **Status Reporting**: Present system status in clear, user-friendly language
- **Preference Management**: Learn and adapt to user communication preferences
- **Multi-turn Conversation**: Maintain context across extended interactions

## Usage

### Ask Questions
Users can ask about system status, recent events, or available capabilities.

### Request Actions
Users can request actions which Romi delegates to Proxy or Ergo as needed.

### Get Help
Users can ask for help understanding system features or troubleshooting steps.

## Dependencies

- Hermes Agent memory system for user preferences
- Knowledge store for reference information
- NATS client for agent communication
