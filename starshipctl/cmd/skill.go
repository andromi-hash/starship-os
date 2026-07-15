package cmd

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"
)

var skillCmd = &cobra.Command{
	Use:   "skill",
	Short: "Manage skills (agent capability modules)",
}

var skillListCmd = &cobra.Command{
	Use:   "list",
	Short: "List installed skills",
	Run: func(cmd *cobra.Command, args []string) {
		projectDir := findProjectRoot()
		skillsDir := filepath.Join(projectDir, "agents", "skills")

		entries, err := os.ReadDir(skillsDir)
		if err != nil {
			fmt.Printf("No skills directory found: %v\n", err)
			return
		}

		for _, entry := range entries {
			if entry.IsDir() {
				skillFile := filepath.Join(skillsDir, entry.Name(), "SKILL.md")
				if _, err := os.Stat(skillFile); err == nil {
					fmt.Printf("  %s\n", entry.Name())
				}
			}
		}
	},
}

var skillShowCmd = &cobra.Command{
	Use:   "show <name>",
	Short: "Show skill definition",
	Args:  cobra.ExactArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		name := args[0]
		projectDir := findProjectRoot()
		skillFile := filepath.Join(projectDir, "agents", "skills", name, "SKILL.md")

		data, err := os.ReadFile(skillFile)
		if err != nil {
			fmt.Printf("Skill '%s' not found\n", name)
			return
		}
		fmt.Println(strings.TrimSpace(string(data)))
	},
}

var skillTriggerCmd = &cobra.Command{
	Use:   "trigger <name>",
	Short: "Trigger a skill via NATS",
	Args:  cobra.ExactArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		name := args[0]
		fmt.Printf("Triggering skill '%s'... (use 'agnetic workflow run' or NATS pub)\n", name)
		fmt.Printf("  nats pub starship.skill.%s '{}'\n", name)
		fmt.Printf("  nats pub agnetic.skill.%s '{}'  # legacy\n", name)
	},
}

func init() {
	skillCmd.AddCommand(skillListCmd)
	skillCmd.AddCommand(skillShowCmd)
	skillCmd.AddCommand(skillTriggerCmd)
	rootCmd.AddCommand(skillCmd)
}
