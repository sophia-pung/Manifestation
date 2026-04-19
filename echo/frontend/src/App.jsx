import { useState, useCallback, useEffect } from 'react'
import { useWebSocket } from './hooks/useWebSocket'
import { StateIndicator } from './components/StateIndicator'
import { SMSThread } from './components/SMSThread'
import { TreeMenu } from './components/TreeMenu'
import { EvalPanel } from './components/EvalPanel'

const BACKEND_WS = 'ws://localhost:8765'

function Timeout({ state, lastActivity }) {
  const [remaining, setRemaining] = useState(null)
  const TIMEOUT = 10

  useEffect(() => {
    if (!['TREE', 'CONFIRM_SEND'].includes(state)) {
      setRemaining(null)
      return
    }
    const update = () => {
      const elapsed = (Date.now() - lastActivity) / 1000
      const r = Math.max(0, TIMEOUT - elapsed)
      setRemaining(Math.ceil(r))
    }
    update()
    const id = setInterval(update, 250)
    return () => clearInterval(id)
  }, [state, lastActivity])

  if (remaining === null) return null
  const pct = remaining / TIMEOUT
  const color = pct > 0.5 ? 'text-gray-500' : pct > 0.25 ? 'text-yellow-500' : 'text-red-500'
  return <span className={`text-xs tabular-nums ${color}`}>{remaining}s</span>
}

export default function App() {
  const [state, setState] = useState('IDLE')
  const [messages, setMessages] = useState([])
  const [node, setNode] = useState(null)
  const [currentOption, setCurrentOption] = useState(0)
  const [path, setPath] = useState([])
  const [finalReply, setFinalReply] = useState(null)
  const [toolLog, setToolLog] = useState([])
  const [evalReport, setEvalReport] = useState(null)
  const [lastActivity, setLastActivity] = useState(Date.now())
  const [demoSmsText, setDemoSmsText] = useState('')
  const [showDemoPanel, setShowDemoPanel] = useState(false)

  const handleMessage = useCallback((msg) => {
    switch (msg.type) {
      case 'state_change':
        setState(msg.state)
        if (['TREE', 'CONFIRM_SEND'].includes(msg.state)) setLastActivity(Date.now())
        break

      case 'sms_received':
        setMessages(prev => [...prev, {
          body: msg.body,
          sender: msg.sender,
          timestamp: msg.timestamp,
          outbound: false,
        }])
        break

      case 'sms_sent':
        setMessages(prev => [...prev, {
          body: msg.text,
          timestamp: msg.timestamp,
          outbound: true,
        }])
        setNode(null)
        setPath([])
        setFinalReply(null)
        break

      case 'tree_node':
        setNode(msg)
        setCurrentOption(0)
        setPath(msg.path_so_far || [])
        setFinalReply(null)
        setLastActivity(Date.now())
        break

      case 'option_changed':
        setCurrentOption(msg.index)
        setLastActivity(Date.now())
        break

      case 'path_updated':
        setPath(msg.path_so_far || [])
        break

      case 'confirm_send':
        setFinalReply(msg)
        setLastActivity(Date.now())
        break

      case 'tool_call':
        setToolLog(prev => [
          { tool: msg.tool, reasoning: msg.reasoning, ts: Date.now() },
          ...prev.slice(0, 9),
        ])
        break

      case 'validation_event':
        setToolLog(prev => [
          { tool: `validate:${msg.passed ? 'pass' : 'fail'}`, reasoning: msg.issue, ts: Date.now() },
          ...prev.slice(0, 9),
        ])
        break

      default:
        break
    }
  }, [])

  const { connected, send } = useWebSocket(handleMessage)

  const sendSignal = (signal) => send({ type: 'keyboard_override', signal })

  const injectSms = () => {
    if (!demoSmsText.trim()) return
    send({ type: 'inject_sms', body: demoSmsText.trim(), sender: 'Demo' })
    setDemoSmsText('')
    setShowDemoPanel(false)
  }

  // Load eval report if available (fetched from backend or served as static)
  useEffect(() => {
    fetch('/eval_report.json').then(r => r.json()).then(setEvalReport).catch(() => {})
  }, [])

  const inMenu = ['TREE', 'CONFIRM_SEND'].includes(state)
  const isProcessing = state === 'PROCESSING'

  return (
    <div className="h-screen flex flex-col max-w-lg mx-auto border-x border-gray-900">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800 bg-gray-950 shrink-0">
        <div className="flex items-center gap-3">
          <span className="text-lg font-bold tracking-wider text-gray-100">ECHO</span>
          <span className="text-xs text-gray-700">BCI Reply System</span>
        </div>
        <div className="flex items-center gap-3">
          {inMenu && <Timeout state={state} lastActivity={lastActivity} />}
          <StateIndicator state={state} connected={connected} />
        </div>
      </div>

      {/* SMS Thread */}
      <div className="flex-1 min-h-0 flex flex-col border-b border-gray-800">
        <SMSThread messages={messages} />
      </div>

      {/* Processing indicator */}
      {isProcessing && (
        <div className="px-4 py-3 border-b border-gray-800 bg-gray-900">
          <div className="flex items-center gap-2 text-yellow-400 text-xs mb-2">
            <span className="animate-spin">◐</span>
            <span>Echo is thinking…</span>
          </div>
          {toolLog.slice(0, 3).map((t, i) => (
            <div key={i} className="flex items-start gap-2 text-xs text-gray-600 truncate">
              <span className="text-purple-500 shrink-0">{t.tool}</span>
              <span className="truncate">{t.reasoning}</span>
            </div>
          ))}
        </div>
      )}

      {/* Tree menu */}
      {(inMenu || state === 'PROCESSING') && (
        <div className="px-4 py-3 border-b border-gray-800 shrink-0">
          {inMenu && (
            <TreeMenu
              node={node}
              currentOption={currentOption}
              path={path}
              finalReply={finalReply}
              state={state}
            />
          )}
        </div>
      )}

      {/* IDLE hint */}
      {state === 'IDLE' && (
        <div className="px-4 py-3 text-xs text-gray-700 border-b border-gray-800 shrink-0">
          Waiting… JAW CLENCH to begin reply
        </div>
      )}

      {/* Tool log (always visible, small) */}
      {toolLog.length > 0 && !isProcessing && (
        <div className="px-4 py-2 border-b border-gray-900 shrink-0">
          <div className="text-xs text-gray-700 flex items-center gap-2 overflow-hidden">
            <span className="text-purple-700 shrink-0">last tool:</span>
            <span className="text-purple-500 shrink-0">{toolLog[0].tool}</span>
            <span className="truncate text-gray-700">{toolLog[0].reasoning}</span>
          </div>
        </div>
      )}

      {/* Keyboard controls */}
      <div className="px-4 py-2 border-b border-gray-800 bg-gray-950 shrink-0">
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-700 mr-1">Override:</span>
          <button onClick={() => sendSignal('clench')}
            className="px-3 py-1 text-xs rounded bg-green-900 hover:bg-green-800 text-green-300 border border-green-800 transition-colors">
            J Clench
          </button>
          <button onClick={() => sendSignal('single_blink')}
            className="px-3 py-1 text-xs rounded bg-blue-900 hover:bg-blue-800 text-blue-300 border border-blue-800 transition-colors">
            B Blink
          </button>
          <button onClick={() => sendSignal('triple_blink')}
            className="px-3 py-1 text-xs rounded bg-red-900 hover:bg-red-800 text-red-300 border border-red-800 transition-colors">
            T Triple
          </button>
          <button onClick={() => setShowDemoPanel(p => !p)}
            className="ml-auto px-3 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 text-gray-400 border border-gray-700 transition-colors">
            SMS
          </button>
        </div>

        {showDemoPanel && (
          <div className="mt-2 flex gap-2">
            <input
              type="text"
              value={demoSmsText}
              onChange={e => setDemoSmsText(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && injectSms()}
              placeholder="Type demo message and press Enter…"
              className="flex-1 bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-xs text-gray-200 placeholder-gray-600 outline-none focus:border-blue-600"
              autoFocus
            />
            <button
              onClick={injectSms}
              className="px-3 py-1.5 text-xs rounded bg-blue-800 hover:bg-blue-700 text-blue-200 border border-blue-700 transition-colors"
            >
              Send
            </button>
          </div>
        )}
      </div>

      {/* Eval panel */}
      <EvalPanel evalReport={evalReport} />
    </div>
  )
}
