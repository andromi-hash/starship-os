.PHONY: all build cli install systemd run-dashboard run-agents clean

all: build

build:
	cd cli && go build -o starship .

cli: build
	cp cli/starship ~/.local/bin/

install: cli
	@echo "=== Installing systemd services ==="
	@bash scripts/install-systemd.sh

run-dashboard:
	nohup python3 dashboard/server.py > logs/dashboard.log 2>&1 &
	@echo "Dashboard started on :8899"

run-agents:
	nohup python3 agents/agent_daemon.py > logs/agents.log 2>&1 &
	@echo "Agents started"

run-history:
	nohup python3 scripts/message_history.py > logs/message-history.log 2>&1 &
	@echo "Message history consumer started"

run-all: run-dashboard run-agents run-history
	@echo "All services started"

stop:
	-pkill -f "server.py"
	-pkill -f "agent_daemon.py"
	-pkill -f "message_history.py"

clean:
	rm -f cli/starship
	rm -rf __pycache__ agents/__pycache__ dashboard/__pycache__

docker:
	docker build -t starship-os .
