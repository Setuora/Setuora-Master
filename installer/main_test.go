package main

import "testing"

func TestOfficialRepository(t *testing.T) {
	if got, want := repositoryURL, "https://github.com/Setuora/Setuora-Master.git"; got != want {
		t.Fatalf("repositoryURL = %q, want %q", got, want)
	}
	if defaultBranch != "main" {
		t.Fatalf("defaultBranch = %q, want main", defaultBranch)
	}
	if defaultInstallFolder != "Setuora-Master" {
		t.Fatalf("defaultInstallFolder = %q, want Setuora-Master", defaultInstallFolder)
	}
}

func TestSetuoraRemoteVariants(t *testing.T) {
	valid := []string{
		"https://github.com/Setuora/Setuora-Master.git",
		"https://github.com/Setuora/Setuora-Master/",
		"git@github.com:Setuora/Setuora-Master.git",
		"ssh://git@github.com/Setuora/Setuora-Master.git",
	}
	for _, remote := range valid {
		if !isSetuoraRemote(remote) {
			t.Errorf("expected valid Setuora remote: %s", remote)
		}
	}
	if isSetuoraRemote("https://github.com/example/Setuora-Master.git") {
		t.Fatal("accepted an unrelated repository")
	}
	if isSetuoraRemote("http://github.com/Setuora/Setuora-Master.git") {
		t.Fatal("accepted an insecure HTTP repository")
	}
	if isSetuoraRemote("https://github.com/Dijo-404/Proj_Setu.git") {
		t.Fatal("accepted the retired monolith repository")
	}
}

func TestPowerShellQuote(t *testing.T) {
	if got, want := powershellQuote(`C:\User's Apps\Setuora.exe`), `'C:\User''s Apps\Setuora.exe'`; got != want {
		t.Fatalf("powershellQuote() = %q, want %q", got, want)
	}
}

func TestWindowsQuoteArgument(t *testing.T) {
	tests := []struct {
		input string
		want  string
	}{
		{"main", "main"},
		{"", `""`},
		{`C:\Setuora`, `C:\Setuora`},
		{`C:\Program Files\Setuora`, `"C:\Program Files\Setuora"`},
		{`C:\folder with spaces\`, `"C:\folder with spaces\\"`},
	}
	for _, test := range tests {
		if got := windowsQuoteArgument(test.input); got != test.want {
			t.Errorf("windowsQuoteArgument(%q) = %q, want %q", test.input, got, test.want)
		}
	}
}

func TestValidBranch(t *testing.T) {
	valid := []string{"main", "release/2026.07", "hotfix-1"}
	for _, branch := range valid {
		if !validBranch.MatchString(branch) {
			t.Errorf("rejected valid branch %q", branch)
		}
	}
	invalid := []string{"-main", "main branch", "main;calc.exe"}
	for _, branch := range invalid {
		if validBranch.MatchString(branch) {
			t.Errorf("accepted invalid branch %q", branch)
		}
	}
}

func TestParseOptionsSupportsUnifiedCommands(t *testing.T) {
	options, err := parseOptions([]string{"start", "--install-dir", `C:\Setuora`})
	if err != nil {
		t.Fatalf("parseOptions() returned %v", err)
	}
	if options.command != "start" {
		t.Fatalf("command = %q, want start", options.command)
	}
	if !options.withCaddy {
		t.Fatal("Caddy should be enabled by default")
	}

	options, err = parseOptions([]string{"repair", "--install-dir", `C:\Setuora`, "--port", "8123"})
	if err != nil {
		t.Fatalf("parseOptions(repair) returned %v", err)
	}
	if options.command != "repair" || options.port != 8123 {
		t.Fatalf("repair options = command %q, port %d", options.command, options.port)
	}

	options, err = parseOptions([]string{"--install-dir", `C:\Setuora`})
	if err != nil {
		t.Fatalf("parseOptions() returned %v", err)
	}
	if options.command != "setup" {
		t.Fatalf("legacy flags command = %q, want setup", options.command)
	}

	options, err = parseOptions([]string{"setup", "--with-caddy=false", "--install-dir", `C:\Setuora`})
	if err != nil {
		t.Fatalf("parseOptions(Caddy opt-out) returned %v", err)
	}
	if options.withCaddy {
		t.Fatal("--with-caddy=false did not disable Caddy")
	}
}
