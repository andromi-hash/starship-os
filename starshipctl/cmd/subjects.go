package cmd

import "strings"

// NATS subject dual-publish helpers (starship.* primary, agnetic.* legacy).
var natsPrefixes = []string{"starship", "agnetic"}

func dualSubjects(subject string) []string {
	rest := subject
	for _, p := range natsPrefixes {
		if subject == p {
			rest = ""
			break
		}
		if strings.HasPrefix(subject, p+".") {
			rest = subject[len(p)+1:]
			break
		}
	}
	out := make([]string, 0, len(natsPrefixes))
	for _, p := range natsPrefixes {
		if rest == "" {
			out = append(out, p)
		} else {
			out = append(out, p+"."+rest)
		}
	}
	return out
}

func primarySubject(parts ...string) string {
	return "starship." + strings.Join(parts, ".")
}
