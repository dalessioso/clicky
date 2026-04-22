//
//  LocalGatewayAudioTranscriptionProvider.swift
//  leanring-buddy
//
//  Localhost-only transcription provider. The Swift app records audio locally,
//  then sends a WAV payload to the Python gateway running on 127.0.0.1:5000.
//

import AVFoundation
import Foundation

struct LocalGatewayAudioTranscriptionProviderError: LocalizedError {
    let message: String

    var errorDescription: String? {
        message
    }
}

final class LocalGatewayAudioTranscriptionProvider: BuddyTranscriptionProvider {
    let displayName = "Local Gateway"
    let requiresSpeechRecognitionPermission = false
    let isConfigured = true
    let unavailableExplanation: String? = nil

    func startStreamingSession(
        keyterms: [String],
        onTranscriptUpdate: @escaping (String) -> Void,
        onFinalTranscriptReady: @escaping (String) -> Void,
        onError: @escaping (Error) -> Void
    ) async throws -> any BuddyStreamingTranscriptionSession {
        LocalGatewayAudioTranscriptionSession(
            keyterms: keyterms,
            onTranscriptUpdate: onTranscriptUpdate,
            onFinalTranscriptReady: onFinalTranscriptReady,
            onError: onError
        )
    }
}

private final class LocalGatewayAudioTranscriptionSession: BuddyStreamingTranscriptionSession {
    private static let stateQueueSpecificKey = DispatchSpecificKey<Void>()

    private struct LocalGatewayTranscriptionRequest: Encodable {
        let audioWAVBase64: String
        let keyterms: [String]

        enum CodingKeys: String, CodingKey {
            case audioWAVBase64 = "audio_wav_base64"
            case keyterms
        }
    }

    private struct LocalGatewayTranscriptionResponse: Decodable {
        let transcript: String
    }

    private static let transcriptionURL = AppBundleConfiguration.localGatewayURL(path: "/transcribe")
    private static let targetSampleRate = 16_000

    let finalTranscriptFallbackDelaySeconds: TimeInterval = 8.0

    private let keyterms: [String]
    private let onTranscriptUpdate: (String) -> Void
    private let onFinalTranscriptReady: (String) -> Void
    private let onError: (Error) -> Void

    private let stateQueue = DispatchQueue(label: "com.clicky.gateway.transcription")
    private let audioPCM16Converter = BuddyPCM16AudioConverter(
        targetSampleRate: Double(LocalGatewayAudioTranscriptionSession.targetSampleRate)
    )
    private let urlSession: URLSession

    private var bufferedPCM16AudioData = Data()
    private var hasRequestedFinalTranscript = false
    private var hasDeliveredFinalTranscript = false
    private var isCancelled = false
    private var transcriptionUploadTask: Task<Void, Never>?

    init(
        keyterms: [String],
        onTranscriptUpdate: @escaping (String) -> Void,
        onFinalTranscriptReady: @escaping (String) -> Void,
        onError: @escaping (Error) -> Void
    ) {
        self.keyterms = keyterms
        self.onTranscriptUpdate = onTranscriptUpdate
        self.onFinalTranscriptReady = onFinalTranscriptReady
        self.onError = onError

        let urlSessionConfiguration = URLSessionConfiguration.default
        urlSessionConfiguration.timeoutIntervalForRequest = 45
        urlSessionConfiguration.timeoutIntervalForResource = 90
        urlSessionConfiguration.waitsForConnectivity = false
        urlSessionConfiguration.urlCache = nil
        urlSessionConfiguration.httpCookieStorage = nil
        self.urlSession = URLSession(configuration: urlSessionConfiguration)
        self.stateQueue.setSpecific(
            key: LocalGatewayAudioTranscriptionSession.stateQueueSpecificKey,
            value: ()
        )
    }

    func appendAudioBuffer(_ audioBuffer: AVAudioPCMBuffer) {
        guard let audioPCM16Data = audioPCM16Converter.convertToPCM16Data(from: audioBuffer),
              !audioPCM16Data.isEmpty else {
            return
        }

        stateQueue.async {
            guard !self.hasRequestedFinalTranscript, !self.isCancelled else { return }
            self.bufferedPCM16AudioData.append(audioPCM16Data)
        }
    }

    func requestFinalTranscript() {
        stateQueue.async {
            guard !self.hasRequestedFinalTranscript, !self.isCancelled else { return }
            self.hasRequestedFinalTranscript = true

            let bufferedPCM16AudioData = self.bufferedPCM16AudioData
            self.transcriptionUploadTask = Task { [weak self] in
                await self?.transcribeBufferedAudio(bufferedPCM16AudioData)
            }
        }
    }

    func cancel() {
        let cancelSessionState = {
            self.isCancelled = true
            self.bufferedPCM16AudioData.removeAll(keepingCapacity: false)
        }

        if DispatchQueue.getSpecific(
            key: LocalGatewayAudioTranscriptionSession.stateQueueSpecificKey
        ) != nil {
            cancelSessionState()
        } else {
            stateQueue.sync(execute: cancelSessionState)
        }

        transcriptionUploadTask?.cancel()
        transcriptionUploadTask = nil
        urlSession.invalidateAndCancel()
    }

    private func transcribeBufferedAudio(_ bufferedPCM16AudioData: Data) async {
        guard !Task.isCancelled else { return }

        let shouldReturnEmptyTranscript = stateQueue.sync {
            isCancelled || bufferedPCM16AudioData.isEmpty
        }

        if shouldReturnEmptyTranscript {
            deliverFinalTranscript("")
            return
        }

        let wavAudioData = BuddyWAVFileBuilder.buildWAVData(
            fromPCM16MonoAudio: bufferedPCM16AudioData,
            sampleRate: Self.targetSampleRate
        )

        do {
            let transcriptText = try await requestTranscription(for: wavAudioData)
            guard !stateQueue.sync(execute: { isCancelled }) else { return }

            if !transcriptText.isEmpty {
                onTranscriptUpdate(transcriptText)
            }

            deliverFinalTranscript(transcriptText)
        } catch {
            guard !stateQueue.sync(execute: { isCancelled }) else { return }
            onError(error)
        }
    }

    private func requestTranscription(for wavAudioData: Data) async throws -> String {
        var request = URLRequest(url: Self.transcriptionURL)
        request.httpMethod = "POST"
        request.timeoutInterval = 45
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let requestBody = LocalGatewayTranscriptionRequest(
            audioWAVBase64: wavAudioData.base64EncodedString(),
            keyterms: normalizedKeyterms()
        )
        request.httpBody = try JSONEncoder().encode(requestBody)

        let (responseData, response) = try await urlSession.data(for: request)

        guard let httpResponse = response as? HTTPURLResponse else {
            throw LocalGatewayAudioTranscriptionProviderError(
                message: "The local transcription gateway returned an invalid response."
            )
        }

        guard (200...299).contains(httpResponse.statusCode) else {
            throw LocalGatewayAudioTranscriptionProviderError(
                message: AppBundleConfiguration.localGatewayFailureMessage(
                    operationDescription: "Local transcription",
                    httpResponse: httpResponse,
                    responseData: responseData
                )
            )
        }

        let transcriptionResponse = try JSONDecoder().decode(
            LocalGatewayTranscriptionResponse.self,
            from: responseData
        )

        return transcriptionResponse.transcript.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func normalizedKeyterms() -> [String] {
        keyterms
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    private func deliverFinalTranscript(_ transcriptText: String) {
        guard !hasDeliveredFinalTranscript else { return }
        hasDeliveredFinalTranscript = true
        onFinalTranscriptReady(transcriptText)
    }

    deinit {
        cancel()
    }
}
