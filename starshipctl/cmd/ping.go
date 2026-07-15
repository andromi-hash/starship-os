package cmd

import (
	"fmt"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/spf13/cobra"
)

var pingCmd = &cobra.Command{
	Use:   "ping",
	Short: "Ping the NATS agent bus",
	Run: func(cmd *cobra.Command, args []string) {
		nc, err := nats.Connect("127.0.0.1:4222", nats.Timeout(3*time.Second))
		if err != nil {
			fmt.Printf("NATS connection failed: %v\n", err)
			return
		}
		defer nc.Close()

		payload := []byte(`{"status":"ok","agent":"cli"}`)
		for _, subj := range dualSubjects(primarySubject("agent", "proxy", "status")) {
			if err = nc.Publish(subj, payload); err != nil {
				fmt.Printf("Publish failed on %s: %v\n", subj, err)
				return
			}
		}

		fmt.Println("NATS bus OK — dual-published to starship.* + agnetic.*")
	},
}

func init() {
	rootCmd.AddCommand(pingCmd)
}
