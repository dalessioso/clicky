//
//  CompanionHistoryView.swift
//  leanring-buddy
//
//  A native SwiftUI scrollable history panel for reviewing previous interactions.
//  Acts as a ChatGPT-style interface displaying the User's prompt and the
//  Assistant's detailed_text fetched from the local gateway.
//

import SwiftUI
import AppKit

struct HistoryItem: Codable, Identifiable {
    let id: Int
    let timestamp: String
    let userPrompt: String
    let assistantDetailedText: String
    let actionCoordinates: String?

    enum CodingKeys: String, CodingKey {
        case id
        case timestamp
        case userPrompt = "user_prompt"
        case assistantDetailedText = "assistant_detailed_text"
        case actionCoordinates = "action_coordinates"
    }
}

struct HistoryResponse: Codable {
    let history: [HistoryItem]
}

struct CompanionHistoryView: View {
    @Binding var isShowing: Bool
    @State private var items: [HistoryItem] = []
    @State private var isLoading = true
    @State private var errorMessage: String? = nil

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
                .background(DS.Colors.borderSubtle)

            if isLoading {
                Spacer()
                ProgressView()
                    .progressViewStyle(CircularProgressViewStyle(tint: DS.Colors.accentText))
                    .scaleEffect(0.8)
                Spacer()
            } else if let errorMessage = errorMessage {
                Spacer()
                Text("Failed to load history:\n\(errorMessage)")
                    .font(.system(size: 12))
                    .foregroundColor(DS.Colors.destructiveText)
                    .multilineTextAlignment(.center)
                    .padding()
                Spacer()
            } else if items.isEmpty {
                Spacer()
                Text("No conversation history yet.")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundColor(DS.Colors.textTertiary)
                Spacer()
            } else {
                ScrollView {
                    VStack(spacing: 16) {
                        ForEach(items) { item in
                            historyMessageBubble(item: item)
                        }
                    }
                    .padding()
                }
            }
        }
        .frame(width: 440, height: 500)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(DS.Colors.background)
        )
        .onAppear {
            fetchHistory()
        }
    }

    private var header: some View {
        HStack {
            Button(action: {
                withAnimation {
                    isShowing = false
                }
            }) {
                Image(systemName: "chevron.left")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundColor(DS.Colors.textSecondary)
                    .frame(width: 24, height: 24)
                    .background(Circle().fill(DS.Colors.surface2))
            }
            .buttonStyle(.plain)
            .pointerCursor()

            Text("Interaction History")
                .font(.system(size: 14, weight: .semibold))
                .foregroundColor(DS.Colors.textPrimary)
                .padding(.leading, 4)

            Spacer()
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }

    private func historyMessageBubble(item: HistoryItem) -> some View {
        VStack(spacing: 12) {
            // User side
            HStack {
                Spacer(minLength: 40)
                Text(item.userPrompt)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundColor(.white)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 10)
                    .background(
                        RoundedRectangle(cornerRadius: DS.CornerRadius.large)
                            .fill(DS.Colors.helpChatUserBubble)
                    )
            }

            // Assistant side
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(item.assistantDetailedText)
                        .font(.system(size: 13))
                        .foregroundColor(DS.Colors.textPrimary)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)

                    HStack {
                        Spacer()
                        Button(action: {
                            let pb = NSPasteboard.general
                            pb.clearContents()
                            pb.setString(item.assistantDetailedText, forType: .string)
                        }) {
                            HStack(spacing: 4) {
                                Image(systemName: "doc.on.doc")
                                    .font(.system(size: 10))
                                Text("Copy to Clipboard")
                                    .font(.system(size: 11, weight: .medium))
                            }
                            .foregroundColor(DS.Colors.textSecondary)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(
                                Capsule()
                                    .fill(DS.Colors.surface3)
                            )
                        }
                        .buttonStyle(.plain)
                        .pointerCursor()
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
                .background(
                    RoundedRectangle(cornerRadius: DS.CornerRadius.large)
                        .fill(DS.Colors.surface2)
                )

                Spacer(minLength: 40)
            }
        }
        .padding(.bottom, 8)
    }

    private func fetchHistory() {
        isLoading = true
        errorMessage = nil

        let historyURL = AppBundleConfiguration.localGatewayURL(path: "history")
        var request = URLRequest(url: historyURL)
        request.httpMethod = "GET"

        let task = URLSession.shared.dataTask(with: request) { data, response, error in
            DispatchQueue.main.async {
                if let error = error {
                    self.errorMessage = error.localizedDescription
                    self.isLoading = false
                    return
                }

                guard let data = data else {
                    self.errorMessage = "No data returned"
                    self.isLoading = false
                    return
                }

                do {
                    let res = try JSONDecoder().decode(HistoryResponse.self, from: data)
                    self.items = res.history
                    self.isLoading = false
                } catch {
                    self.errorMessage = "Failed to decode JSON: \(error.localizedDescription)"
                    self.isLoading = false
                }
            }
        }
        task.resume()
    }
}
