const STATE_CONFIG = {
  IDLE:         { label: 'IDLE',         color: 'text-gray-400',   dot: 'bg-gray-500' },
  PROCESSING:   { label: 'THINKING',     color: 'text-yellow-400', dot: 'bg-yellow-400 animate-pulse' },
  TREE:         { label: 'MENU',         color: 'text-blue-400',   dot: 'bg-blue-400' },
  CONFIRM_SEND: { label: 'CONFIRM SEND', color: 'text-green-400',  dot: 'bg-green-400 animate-pulse' },
  SENDING:      { label: 'SENDING',      color: 'text-purple-400', dot: 'bg-purple-400 animate-pulse' },
}

export function StateIndicator({ state, connected }) {
  const cfg = STATE_CONFIG[state] || STATE_CONFIG.IDLE

  return (
    <div className="flex items-center gap-4 text-xs">
      <span className={`flex items-center gap-1.5 ${connected ? 'text-emerald-400' : 'text-red-400'}`}>
        <span className={`w-2 h-2 rounded-full ${connected ? 'bg-emerald-400' : 'bg-red-400'}`} />
        {connected ? 'CONNECTED' : 'DISCONNECTED'}
      </span>
      <span className={`flex items-center gap-1.5 font-bold tracking-widest ${cfg.color}`}>
        <span className={`w-2 h-2 rounded-full ${cfg.dot}`} />
        {cfg.label}
      </span>
    </div>
  )
}
