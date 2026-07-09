package cmd

import (
	"encoding/json"
	"fmt"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/spf13/cobra"
)

var workflowCmd = &cobra.Command{
	Use:   "workflow",
	Short: "Multi-agent workflow orchestration",
}

var workflowRunCmd = &cobra.Command{
	Use:   "run <name>",
	Short: "Run a multi-agent workflow",
	Long:  `Run a workflow that coordinates multiple agents. Workflows: security-audit, deploy, system-health`,
	Args:  cobra.ExactArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		name := args[0]
		payload, _ := cmd.Flags().GetString("payload")

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

		subject := fmt.Sprintf("starship.workflow.%s", name)
		msg := fmt.Sprintf(`{"workflow":"%s","payload":%s}`, name, payload)

		if err := nc.PublishRequest(subject, inbox, []byte(msg)); err != nil {
			fmt.Printf("Publish failed: %v\n", err)
			return
		}

		reply, err := sub.NextMsg(30 * time.Second)
		if err != nil {
			fmt.Printf("No response from workflow engine: %v\n", err)
			return
		}

		var result map[string]interface{}
		if err := json.Unmarshal(reply.Data, &result); err != nil {
			fmt.Printf("Parse error: %v\n", err)
			return
		}
		out, _ := json.MarshalIndent(result, "", "  ")
		fmt.Println(string(out))
	},
}

var workflowListCmd = &cobra.Command{
	Use:   "list",
	Short: "List available workflows",
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println("Available workflows:")
		fmt.Println("  security-audit  - Full security audit across all agents")
		fmt.Println("  deploy          - Review, test, and deploy code")
		fmt.Println("  system-health   - Health check across all agents")
	},
}

func init() {
	workflowRunCmd.Flags().StringP("payload", "p", "{}", "JSON payload")
	workflowCmd.AddCommand(workflowRunCmd)
	workflowCmd.AddCommand(workflowListCmd)
	rootCmd.AddCommand(workflowCmd)
}
