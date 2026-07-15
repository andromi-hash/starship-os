package cmd

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"strings"

	"github.com/spf13/cobra"
)

var tuiCmd = &cobra.Command{
	Use:   "tui",
	Short: "Interactive Starship OS TUI shell",
	Long:  "Primary text UI: status, fleet, agents, smoke helpers without leaving the terminal",
	Run:   runTUI,
}

func init() {
	rootCmd.AddCommand(tuiCmd)
}

func runTUI(cmd *cobra.Command, args []string) {
	in := bufio.NewReader(os.Stdin)
	fmt.Println("╔══════════════════════════════════════════════╗")
	fmt.Println("║  Starship OS TUI  ·  type help · quit to exit ║")
	fmt.Println("╚══════════════════════════════════════════════╝")
	printTUIHelp()

	for {
		fmt.Print("starship> ")
		line, err := in.ReadString('\n')
		if err != nil {
			fmt.Println()
			return
		}
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		parts := strings.Fields(line)
		switch strings.ToLower(parts[0]) {
		case "q", "quit", "exit":
			fmt.Println("bye")
			return
		case "h", "help", "?":
			printTUIHelp()
		case "status":
			_ = runMake("status")
		case "fleet":
			sub := "status"
			if len(parts) > 1 {
				sub = parts[1]
			}
			_ = runSelf("fleet", sub)
		case "plants":
			_ = runSelf("fleet", "plants")
		case "version":
			_ = runSelf("version")
		case "ping":
			_ = runSelf("ping")
		case "agents":
			_ = runSelf("agent", "status")
		case "health":
			_ = runSelf("system", "health")
		case "smoke":
			_ = runMake("smoke")
		case "dashboard":
			fmt.Println("Dashboard: http://localhost:8788")
			fmt.Println("Fleet API: http://localhost:8788/api/fleet")
		case "opencode":
			printOpenCodeHint()
		case "clear", "cls":
			fmt.Print("\033[H\033[2J")
		default:
			// Pass-through: starshipctl <args...>
			_ = runSelf(parts...)
		}
	}
}

func printTUIHelp() {
	fmt.Println(`Commands:
  status          make status (services + models)
  fleet [cmd]     fleet status|plants|nodes|register
  plants          fleet plants
  agents          agent status
  health          system health
  ping            NATS ping
  version         CLI version
  smoke           run smoke tests
  dashboard       print C2 URL
  opencode        OpenCode pantheon hint
  clear           clear screen
  help            this help
  quit            exit TUI
  <any>           pass through to starshipctl`)
}

func printOpenCodeHint() {
	fmt.Println("OpenCode pantheon (coding — not red-team):")
	fmt.Println("  config: /etc/starship/opencode/oh-my-opencode-slim.json")
	fmt.Println("  install: bash scripts/install-opencode.sh")
	fmt.Println("  policy: red-team never unrestricted OpenCode (fleet_policy)")
	if _, err := exec.LookPath("opencode"); err == nil {
		out, _ := exec.Command("opencode", "--version").CombinedOutput()
		fmt.Printf("  opencode: %s\n", strings.TrimSpace(string(out)))
	} else {
		fmt.Println("  opencode: not on PATH")
	}
}

func runSelf(args ...string) error {
	exe, err := os.Executable()
	if err != nil {
		exe = "starshipctl"
	}
	c := exec.Command(exe, args...)
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	c.Stdin = os.Stdin
	return c.Run()
}

func runMake(target string) error {
	c := exec.Command("make", target)
	if root := findProjectRoot(); root != "" {
		c.Dir = root
	}
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	return c.Run()
}
