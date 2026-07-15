package cmd

import (
	"encoding/json"
	"fmt"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/spf13/cobra"
)

type Telemetry struct {
	Cpu         float32 `json:"cpu"`
	MemoryUsed  uint64  `json:"memory_used"`
	MemoryTotal uint64  `json:"memory_total"`
	DiskUsed    uint64  `json:"disk_used"`
	DiskTotal   uint64  `json:"disk_total"`
	RxBytes     uint64  `json:"rx_bytes"`
	TxBytes     uint64  `json:"tx_bytes"`
	Timestamp   uint64  `json:"timestamp"`
}

var telemetryCmd = &cobra.Command{
	Use:   "telemetry",
	Short: "Show latest StarAgent telemetry",
	Run: func(cmd *cobra.Command, args []string) {
		nc, err := nats.Connect("127.0.0.1:4222", nats.Timeout(3*time.Second))
		if err != nil {
			fmt.Printf("NATS connection failed: %v\n", err)
			return
		}
		defer nc.Close()

		sub, err := nc.SubscribeSync(primarySubject("telemetry"))
		if err != nil {
			fmt.Printf("Subscribe failed: %v\n", err)
			return
		}

		msg, err := sub.NextMsg(15 * time.Second)
		if err != nil {
			fmt.Printf("No telemetry received: %v\n", err)
			return
		}

		var t Telemetry
		if err := json.Unmarshal(msg.Data, &t); err != nil {
			fmt.Printf("Parse error: %v\n", err)
			return
		}

		ts := time.Unix(int64(t.Timestamp), 0).Format(time.RFC3339)
		fmt.Printf("CPU:    %.1f%%\n", t.Cpu)
		fmt.Printf("Memory: %d MB / %d MB\n", t.MemoryUsed/1024/1024, t.MemoryTotal/1024/1024)
		fmt.Printf("Disk:   %d GB / %d GB\n", t.DiskUsed/1024/1024/1024, t.DiskTotal/1024/1024/1024)
		fmt.Printf("Net RX: %d KB\n", t.RxBytes/1024)
		fmt.Printf("Net TX: %d KB\n", t.TxBytes/1024)
		fmt.Printf("Time:   %s\n", ts)
	},
}

func init() {
	rootCmd.AddCommand(telemetryCmd)
}
