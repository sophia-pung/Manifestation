import { useEffect, useRef } from 'react'

function formatTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export function SMSThread({ messages }) {
  const bottomRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  if (!messages.length) {
    return (
      <div className="flex-1 flex items-center justify-center text-gray-600 text-sm">
        Waiting for incoming message…
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
      {messages.map((m, i) => (
        <div key={i} className={`flex ${m.outbound ? 'justify-end' : 'justify-start'}`}>
          <div className="max-w-xs">
            {!m.outbound && (
              <div className="text-xs text-gray-500 mb-1 px-1">{m.sender}</div>
            )}
            <div
              className={`rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${
                m.outbound
                  ? 'bg-blue-600 text-white rounded-br-sm'
                  : 'bg-gray-800 text-gray-100 rounded-bl-sm'
              }`}
            >
              {m.body}
            </div>
            <div className={`text-xs text-gray-600 mt-1 px-1 ${m.outbound ? 'text-right' : ''}`}>
              {formatTime(m.timestamp)}
            </div>
          </div>
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
