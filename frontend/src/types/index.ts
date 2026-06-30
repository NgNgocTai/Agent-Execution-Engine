export type AgentEventType =
  | "thinking"
  | "tool_call_start"
  | "tool_call_result"
  | "approval_required"
  | "text_delta"
  | "final_answer"
  | "conversation_id"
  | "error";

export interface AgentEvent {
  event_id: string;
  type: AgentEventType;
  data: Record<string, unknown>;
  timestamp_ms: number;
  session_id: string | null;
  iteration: number | null;
}

export interface ToolCallCard {
  callId: string;
  toolName: string;
  arguments: Record<string, unknown>;
  status: "loading" | "success" | "error" | "approval_required";
  result: Record<string, unknown> | string | null;
  error: string | null;
  executionTimeMs: number;
  showInput: boolean;
  approval?: ApprovalRequest | null;
}

export interface ApprovalRequest {
  host?: string;
  service?: string;
  action?: string;
  command?: string;
  reason?: string;
  expires_at?: string;
  metrics?: Record<string, unknown>;
  thresholds?: Record<string, unknown>;
}

export interface ThinkingBlock {
  content: string;
  isExpanded: boolean;
}

export interface ProviderOption {
  id: string;
  label: string;
  color: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  thinkingBlocks: ThinkingBlock[];
  toolCallCards: ToolCallCard[];
  textContent: string;
  isStreaming: boolean;
  timestamp: string;
}
