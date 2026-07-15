package cmd

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/spf13/cobra"
)

var agentCmd = &cobra.Command{
	Use:   "agent",
	Short: "Manage Hermes agents (proxy, romi, ergo)",
}

var agentRunCmd = &cobra.Command{
	Use:   "run [name]",
	Short: "Start an agent daemon",
	Long:  `Start a Hermes agent daemon. Agents: proxy, romi, ergo.`,
	Args:  cobra.MaximumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		name := "proxy"
		if len(args) > 0 {
			name = args[0]
		}
		model, _ := cmd.Flags().GetString("model")

		projectDir := findProjectRoot()
		script := filepath.Join(projectDir, "agents", "run_agent.sh")
		if _, err := os.Stat(script); os.IsNotExist(err) {
			fmt.Printf("Agent script not found: %s\n", script)
			return
		}

		agentCmd := exec.Command("/bin/bash", script, name)
		if model != "" {
			agentCmd = exec.Command("/bin/bash", script, name, "--model", model)
		}
		agentCmd.Stdout = os.Stdout
		agentCmd.Stderr = os.Stderr
		if err := agentCmd.Run(); err != nil {
			fmt.Printf("Failed to start agent '%s': %v\n", name, err)
		}
	},
}

var agentStatusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show agent daemon status",
	Run: func(cmd *cobra.Command, args []string) {
		projectDir := findProjectRoot()

		for _, name := range []string{"proxy", "romi", "ergo"} {
			pidFile := filepath.Join(projectDir, "agents", fmt.Sprintf(".%s.pid", name))
			pidBytes, err := os.ReadFile(pidFile)
			if err != nil {
				fmt.Printf("✗ %-8s stopped\n", name)
				continue
			}
			pid := strings.TrimSpace(string(pidBytes))
			if isRunning(pid) {
				logFile := filepath.Join(projectDir, "logs", fmt.Sprintf("%s.log", name))
				fmt.Printf("✓ %-8s running (PID %s)\n", name, pid)
				fmt.Printf("  Log: %s\n", logFile)
			} else {
				fmt.Printf("✗ %-8s stopped (stale PID %s)\n", name, pid)
				os.Remove(pidFile)
			}
		}

		if isRunningByName("staragent") {
			fmt.Printf("✓ %-8s running\n", "staragent")
		} else {
			fmt.Printf("✗ %-8s stopped\n", "staragent")
		}
	},
}

var agentStopCmd = &cobra.Command{
	Use:   "stop",
	Short: "Stop all agent daemons",
	Run: func(cmd *cobra.Command, args []string) {
		projectDir := findProjectRoot()
		script := filepath.Join(projectDir, "agents", "run_agent.sh")

		agentCmd := exec.Command("/bin/bash", script, "stop")
		agentCmd.Stdout = os.Stdout
		agentCmd.Stderr = os.Stderr
		if err := agentCmd.Run(); err != nil {
			fmt.Printf("Failed to stop agents: %v\n", err)
		}
	},
}

var agentChatCmd = &cobra.Command{
	Use:   "chat <name>",
	Short: "Interactive chat session with an agent",
	Long:  `Open an interactive chat session with an agent (proxy, romi, ergo) via NATS request-reply.`,
	Args:  cobra.ExactArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		name := args[0]

		nc, err := nats.Connect("127.0.0.1:4222", nats.Timeout(3*time.Second))
		if err != nil {
			fmt.Printf("NATS connection failed: %v\n", err)
			return
		}
		defer nc.Close()

		inbox := nc.NewRespInbox()
		sub, err := nc.SubscribeSync(inbox)
		if err != nil {
			fmt.Printf("Subscribe failed: %v\n", err)
			return
		}
		nc.Flush()

		fmt.Printf("Connecting to %s... (type '/exit' to quit)\n\n", name)

		scanner := bufio.NewScanner(os.Stdin)
		for {
			fmt.Printf("\033[33m%s> \033[0m", name)
			if !scanner.Scan() {
				break
			}
			line := strings.TrimSpace(scanner.Text())
			if line == "" {
				continue
			}
			if line == "/exit" || line == "/quit" {
				fmt.Println("Exiting.")
				break
			}

			subject := primarySubject("agent", name, "command", "chat")
			msg := fmt.Sprintf(`{"command":"%s","args":{}}`, line)

			if err := nc.PublishRequest(subject, inbox, []byte(msg)); err != nil {
				fmt.Printf("Publish failed: %v\n", err)
				continue
			}

			reply, err := sub.NextMsg(30 * time.Second)
			if err != nil {
				fmt.Printf("No response from agent (timeout): %v\n", err)
				continue
			}

			var result map[string]interface{}
			if err := json.Unmarshal(reply.Data, &result); err != nil {
				fmt.Printf("Parse error: %v\n", err)
				continue
			}

			response, _ := result["response"].(string)
			if response != "" {
				fmt.Printf("\033[36m%s\033[0m\n", response)
			} else {
				fmt.Printf("%s\n", string(reply.Data))
			}
			fmt.Println()
		}
	},
}

var agentSendCmd = &cobra.Command{
	Use:   "send <agent> <command>",
	Short: "Send a command to an agent via NATS",
	Args:  cobra.ExactArgs(2),
	Run: func(cmd *cobra.Command, args []string) {
		name := args[0]
		command := args[1]
		payload, _ := cmd.Flags().GetString("payload")

		nc, err := nats.Connect("127.0.0.1:4222", nats.Timeout(3*time.Second))
		if err != nil {
			fmt.Printf("NATS connection failed: %v\n", err)
			return
		}
		defer nc.Close()

		subject := primarySubject("agent", name, "command", strings.ReplaceAll(command, " ", "."))
		msg := fmt.Sprintf(`{"command":"%s","args":%s}`, command, payload)
		payloadBytes := []byte(msg)
		for _, subj := range dualSubjects(subject) {
			if err := nc.Publish(subj, payloadBytes); err != nil {
				fmt.Printf("Publish failed on %s: %v\n", subj, err)
				return
			}
		}
		fmt.Printf("Sent command '%s' to agent '%s' on %s (+ agnetic.* dual)\n", command, name, subject)
	},
}

func findProjectRoot() string {
	dir, _ := os.Getwd()
	for {
		if _, err := os.Stat(filepath.Join(dir, "agents")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return dir
		}
		dir = parent
	}
}

func isRunning(pidStr string) bool {
	pid := strings.TrimSpace(pidStr)
	if pid == "" {
		return false
	}
	i, err := strconv.Atoi(pid)
	if err != nil || i <= 0 {
		return false
	}
	return syscall.Kill(i, syscall.Signal(0)) == nil
}

func isRunningByName(name string) bool {
	cmd := exec.Command("pgrep", "-x", name)
	return cmd.Run() == nil
}

func init() {
	agentRunCmd.Flags().StringP("model", "m", "", "Override model (e.g. qwen2.5:7b)")
	agentSendCmd.Flags().StringP("payload", "p", "{}", "JSON payload arguments")
	agentCmd.AddCommand(agentRunCmd)
	agentCmd.AddCommand(agentStatusCmd)
	agentCmd.AddCommand(agentStopCmd)
	agentCmd.AddCommand(agentSendCmd)
	agentCmd.AddCommand(agentChatCmd)
	rootCmd.AddCommand(agentCmd)
}
