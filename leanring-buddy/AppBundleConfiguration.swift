//
//  AppBundleConfiguration.swift
//  leanring-buddy
//
//  Shared helper for reading runtime configuration from the built app bundle.
//

import Foundation

enum AppBundleConfiguration {
    static let localGatewayBaseURL = URL(string: "http://127.0.0.1:5000")!

    static func localGatewayURL(path: String) -> URL {
        let normalizedPath = path.hasPrefix("/") ? String(path.dropFirst()) : path
        return localGatewayBaseURL.appendingPathComponent(normalizedPath)
    }

    static func stringValue(forKey key: String) -> String? {
        if let value = Bundle.main.object(forInfoDictionaryKey: key) as? String {
            let trimmedValue = value.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmedValue.isEmpty {
                return trimmedValue
            }
        }

        guard let resourceInfoPath = Bundle.main.path(forResource: "Info", ofType: "plist"),
              let resourceInfo = NSDictionary(contentsOfFile: resourceInfoPath),
              let value = resourceInfo[key] as? String else {
            return nil
        }

        let trimmedValue = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmedValue.isEmpty ? nil : trimmedValue
    }

    static func localGatewayConflictMessage(
        for httpResponse: HTTPURLResponse
    ) -> String? {
        guard let serverHeader = httpResponse.value(forHTTPHeaderField: "Server"),
              !serverHeader.isEmpty else {
            return nil
        }

        if serverHeader.localizedCaseInsensitiveContains("AirTunes") {
            return "macOS AirPlay Receiver is occupying 127.0.0.1:5000. Turn off AirPlay Receiver or move LoClicky's gateway to a different localhost port."
        }

        if serverHeader.localizedCaseInsensitiveContains("uvicorn") {
            return nil
        }

        return "Another local service (\(serverHeader)) is responding on 127.0.0.1:5000 instead of LoClicky's gateway."
    }

    static func localGatewayFailureMessage(
        operationDescription: String,
        httpResponse: HTTPURLResponse,
        responseData: Data
    ) -> String {
        if let localGatewayConflictMessage = localGatewayConflictMessage(for: httpResponse) {
            return "\(operationDescription) could not reach LoClicky's local gateway. \(localGatewayConflictMessage)"
        }

        let responseText = String(data: responseData, encoding: .utf8)?.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        let diagnosticSuffix = responseText?.isEmpty == false ? ": \(responseText!)" : "."
        return "\(operationDescription) failed (HTTP \(httpResponse.statusCode))\(diagnosticSuffix)"
    }
}
