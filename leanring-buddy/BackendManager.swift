//
//  BackendManager.swift
//  leanring-buddy
//
//  Manages the lifecycle of the bundled Python gateway binary. Launches
//  gateway-server-mac silently as a child process on app start and
//  terminates it cleanly on app quit.
//
//  The gateway binary emits structured [SETUP] lines to stdout during
//  first-boot model downloads. BackendManager parses these and publishes
//  setup progress so the UI can show a loading indicator.
//

import Combine
import Foundation

@MainActor
final class BackendManager: ObservableObject {

    /// Whether the gateway process is currently running and ready to serve.
    @Published private(set) var isGatewayReady: Bool = false

    /// Whether the gateway is performing first-boot setup (downloading models).
    @Published private(set) var isPerformingFirstBootSetup: Bool = false

    /// Human-readable status message from the gateway's stdout during setup.
    @Published private(set) var setupStatusMessage: String = ""

    /// Any error message from a failed setup step.
    @Published private(set) var setupErrorMessage: String? = nil

    private var gatewayProcess: Process?
    private var stdoutPipe: Pipe?

    // MARK: - Lifecycle

    /// Locates the bundled gateway-server-mac binary in the app's Resources
    /// folder and launches it as a background child process. Monitors stdout
    /// for [SETUP] progress lines emitted during first-boot model downloads.
    func launchGateway() {
        // Prevent double-launch
        guard gatewayProcess == nil else { return }

        guard let binaryPath = Bundle.main.path(forResource: "gateway-server-mac", ofType: nil) else {
            print("⚠️ BackendManager: gateway-server-mac not found in app bundle Resources.")
            // Fall through gracefully — the gateway might already be running
            // externally during development (python server.py).
            checkIfGatewayAlreadyRunning()
            return
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: binaryPath)

        // The gateway reads config.json relative to its own location, but
        // PyInstaller extracts --add-data files next to the binary, so the
        // working directory should be the binary's parent.
        let binaryDirectory = (binaryPath as NSString).deletingLastPathComponent
        process.currentDirectoryURL = URL(fileURLWithPath: binaryDirectory)

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice

        // Mark setup as in-progress until we see "[SETUP] Gateway ready."
        isPerformingFirstBootSetup = true
        setupStatusMessage = "Starting secure gateway…"

        // Read stdout asynchronously on a background queue to avoid blocking
        // the main actor. Dispatch UI updates back to main.
        pipe.fileHandleForReading.readabilityHandler = { [weak self] fileHandle in
            let data = fileHandle.availableData
            guard !data.isEmpty else { return }

            if let outputLine = String(data: data, encoding: .utf8) {
                // Parse each line for structured setup progress markers
                for line in outputLine.components(separatedBy: .newlines) {
                    let trimmedLine = line.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !trimmedLine.isEmpty else { continue }

                    DispatchQueue.main.async {
                        self?.handleGatewayOutputLine(trimmedLine)
                    }
                }
            }
        }

        // Handle process termination (unexpected crash)
        process.terminationHandler = { [weak self] terminatedProcess in
            DispatchQueue.main.async {
                print("⚠️ BackendManager: Gateway process exited with code \(terminatedProcess.terminationStatus)")
                self?.isGatewayReady = false
                self?.gatewayProcess = nil
            }
        }

        do {
            try process.run()
            gatewayProcess = process
            stdoutPipe = pipe
            print("🎯 BackendManager: Launched gateway-server-mac (PID \(process.processIdentifier))")
        } catch {
            print("⚠️ BackendManager: Failed to launch gateway: \(error)")
            isPerformingFirstBootSetup = false
            setupErrorMessage = "Failed to start gateway: \(error.localizedDescription)"
        }
    }

    /// Cleanly terminates the gateway process. Called from
    /// applicationWillTerminate to ensure no orphan processes remain.
    func terminateGateway() {
        guard let process = gatewayProcess, process.isRunning else { return }

        // SIGTERM first to allow graceful shutdown
        process.terminate()

        // Give it 2 seconds to exit cleanly, then force kill
        DispatchQueue.global().asyncAfter(deadline: .now() + 2.0) {
            if process.isRunning {
                process.interrupt()  // SIGINT
            }
        }

        stdoutPipe?.fileHandleForReading.readabilityHandler = nil
        gatewayProcess = nil
        stdoutPipe = nil
        isGatewayReady = false
        print("🎯 BackendManager: Gateway process terminated.")
    }

    // MARK: - Stdout Parsing

    /// Parses a single line from the gateway's stdout. Lines prefixed with
    /// [SETUP] indicate first-boot model download progress. The special
    /// line "[SETUP] Gateway ready." signals that all models are loaded
    /// and the HTTP server is about to start.
    private func handleGatewayOutputLine(_ line: String) {
        if line.hasPrefix("[SETUP]") {
            let message = String(line.dropFirst("[SETUP] ".count))

            if message.starts(with: "Gateway ready") {
                isPerformingFirstBootSetup = false
                isGatewayReady = true
                setupStatusMessage = ""
            } else {
                setupStatusMessage = message
            }
        } else if line.hasPrefix("[SETUP_ERROR]") {
            let errorDetail = String(line.dropFirst("[SETUP_ERROR] ".count))
            setupErrorMessage = errorDetail
        } else if line.hasPrefix("[SETUP_WARN]") {
            let warningDetail = String(line.dropFirst("[SETUP_WARN] ".count))
            if warningDetail.contains("Ollama not reachable") {
                setupErrorMessage = "Ollama is not running. Please start Ollama to continue."
            } else {
                setupStatusMessage = warningDetail
            }
        }
    }

    // MARK: - Development Fallback

    /// When running from Xcode without the bundled binary (development mode),
    /// check if the gateway is already running externally (python server.py).
    private func checkIfGatewayAlreadyRunning() {
        let healthURL = AppBundleConfiguration.localGatewayURL(path: "health")
        var request = URLRequest(url: healthURL)
        request.httpMethod = "GET"
        request.timeoutInterval = 2.0

        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                if let httpResponse = response as? HTTPURLResponse,
                   httpResponse.statusCode == 200 {
                    print("🎯 BackendManager: External gateway already running.")
                    self?.isGatewayReady = true
                    self?.isPerformingFirstBootSetup = false
                } else if let httpResponse = response as? HTTPURLResponse,
                          let gatewayConflictMessage = AppBundleConfiguration.localGatewayConflictMessage(
                            for: httpResponse
                          ) {
                    print("⚠️ BackendManager: \(gatewayConflictMessage)")
                    self?.isPerformingFirstBootSetup = false
                    self?.setupErrorMessage = gatewayConflictMessage
                } else {
                    print("⚠️ BackendManager: No bundled binary and no external gateway detected.")
                    self?.isPerformingFirstBootSetup = false
                    self?.setupErrorMessage = "Gateway not available. Run 'python server.py' manually."
                }
            }
        }.resume()
    }
}
