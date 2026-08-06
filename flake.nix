{
  description = "GACA — reproducible environment (Hermes agent + gaca-api)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";
    hermes-agent.url = "github:NousResearch/hermes-agent";

    # --- uv2nix stack: turns a uv workspace (pyproject.toml + uv.lock) into a
    # Nix-built Python environment, no manual dependency packaging. Three inputs
    # that work together:
    #   pyproject-nix           — parses pyproject.toml / uv.lock, builds packages
    #   uv2nix                  — reads the uv.lock and generates the overlay
    #   pyproject-build-systems — provides build backends (hatchling, setuptools…)
    # We pin each to *our* nixpkgs with `follows` so everything uses one nixpkgs.
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    { self, nixpkgs, hermes-agent, pyproject-nix, uv2nix, pyproject-build-systems }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      lib = nixpkgs.lib;

      # Interpreter the packaged app runs on. gaca-api requires >=3.12; 3.12 has
      # the widest wheel coverage on PyPI, which is what uv2nix pulls.
      python = pkgs.python312;

      ####################################################################
      # gaca-api, built from its uv workspace
      ####################################################################

      # 1. Load the workspace. Points at the dir holding pyproject.toml + uv.lock.
      workspace = uv2nix.lib.workspace.loadWorkspace {
        workspaceRoot = ./gaca-api;
      };

      # 2. Turn the locked deps into a nixpkgs overlay. "wheel" = prefer prebuilt
      #    wheels over source builds (faster); pyproject-nix patchelfs the
      #    manylinux wheels so they run under Nix.
      overlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };

      # 3. Escape hatch for per-package fixups (missing build deps, etc.). Empty
      #    for now — add entries here only if a specific dep fails to build.
      pyprojectOverrides = _final: _prev: { };

      # 4. Assemble the Python package set: base builder + build backends +
      #    our locked deps + any overrides, composed in order.
      pythonSet =
        (pkgs.callPackage pyproject-nix.build.packages { inherit python; }).overrideScope
          (lib.composeManyExtensions [
            pyproject-build-systems.overlays.default
            overlay
            pyprojectOverrides
          ]);

      # 5. The deliverable: a self-contained virtualenv with gaca-api + its
      #    default (non-dev) deps installed. Contains bin/gaca-api (our console
      #    entry point). This is what `nix build` yields and what a systemd
      #    ExecStart will point at later.
      gaca-api = pythonSet.mkVirtualEnv "gaca-api-env" workspace.deps.default;

      ####################################################################
      # Hermes
      ####################################################################

      hermes = hermes-agent.packages.${system}.default;

      # `nix run .#hermes` should just start the gateway, so wrap the binary with
      # its subcommand. Extra args still pass through: `nix run .#hermes -- --help`.
      hermes-gateway = pkgs.writeShellScriptBin "hermes-gateway" ''
        # Batteries-included policy, enforced on every launch (idempotent, self-
        # healing — re-asserted each start so config can't drift).
        #
        # Approvals: headless there's no human to answer prompts, so the default
        # `manual` mode stalls on gated tools like `terminal`/`curl`. `off`
        # bypasses the prompts (Hermes still refuses catastrophic commands via
        # its hardline floor).
        ${hermes}/bin/hermes config set approvals.mode off
        # Web search backend: pick the fast structured-search provider so the
        # agent doesn't fall back to (slow / Nix-broken) browser scraping. This
        # only names the backend — the API key itself is a SECRET and stays in
        # ~/.hermes/.env (TAVILY_API_KEY=...), never in the flake (ADR-0001).
        # To switch providers, change this name and set the matching key in .env.
        ${hermes}/bin/hermes config set web.backend tavily
        exec ${hermes}/bin/hermes gateway run "$@"
      '';
    in
    {
      # `nix build .#gaca-api` / `.#hermes`. default = the API.
      packages.${system} = {
        default = gaca-api;
        gaca-api = gaca-api;
        hermes = hermes;
      };

      # `nix run .#api` starts the server; `nix run .#hermes` starts the gateway.
      apps.${system} = {
        api = {
          type = "app";
          program = "${gaca-api}/bin/gaca-api";
        };
        hermes = {
          type = "app";
          program = "${hermes-gateway}/bin/hermes-gateway";
        };
      };

      # `nix develop` — an environment only, never a running service. Gives you
      # hermes, uv, and a matching Python for hacking on gaca-api with the usual
      # uv workflow.
      devShells.${system}.default = pkgs.mkShell {
        packages = [ hermes pkgs.uv python ];
      };
    };
}
