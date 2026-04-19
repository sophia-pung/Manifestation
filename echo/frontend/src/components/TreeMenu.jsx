const NODE_TYPE_LABEL = {
  confirm_intent: 'Intent Check',
  generate_decision_node: 'Decision',
  generate_final_reply: 'Final Reply',
}

function Breadcrumb({ path }) {
  if (!path.length) return null
  return (
    <div className="flex items-center gap-1 text-xs text-gray-500 mb-3 flex-wrap">
      {path.map((p, i) => (
        <span key={i} className="flex items-center gap-1">
          {i > 0 && <span className="text-gray-700">›</span>}
          <span className="bg-gray-800 px-2 py-0.5 rounded text-gray-400">
            {p.selected_label}
          </span>
        </span>
      ))}
    </div>
  )
}

export function TreeMenu({ node, currentOption, path, finalReply, state }) {
  if (state === 'CONFIRM_SEND' && finalReply) {
    return (
      <div className="p-4 border border-green-800 rounded-xl bg-gray-900">
        <div className="text-xs text-green-500 uppercase tracking-widest mb-3">
          Confirm Reply
        </div>
        <div className="text-gray-100 text-sm leading-relaxed bg-gray-800 rounded-lg px-4 py-3 mb-4">
          {finalReply.reply_text}
        </div>
        <div className="flex gap-6 text-xs text-gray-500">
          <span><span className="text-green-400 font-bold">CLENCH</span> to send</span>
          <span><span className="text-red-400 font-bold">TRIPLE BLINK</span> to revise</span>
        </div>
      </div>
    )
  }

  if (!node) return null

  return (
    <div className="p-4 border border-gray-800 rounded-xl bg-gray-900">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs text-blue-500 uppercase tracking-widest">
          {NODE_TYPE_LABEL[node.node_type] || 'Question'}
        </span>
        <span className="text-xs text-gray-600">
          depth {node.depth}
        </span>
      </div>

      <Breadcrumb path={path || []} />

      <div className="text-gray-200 text-sm font-medium mb-4">
        {node.question}
      </div>

      <div className="space-y-2">
        {(node.options || []).map((opt, i) => (
          <div
            key={i}
            className={`flex items-center gap-3 px-4 py-3 rounded-lg border transition-all duration-150 ${
              i === currentOption
                ? 'border-blue-500 bg-blue-950 option-active text-blue-100'
                : 'border-gray-700 bg-gray-800 text-gray-400'
            }`}
          >
            <span className={`text-xs font-bold w-4 ${i === currentOption ? 'text-blue-400' : 'text-gray-600'}`}>
              {i === currentOption ? '►' : ' '}
            </span>
            <span className="text-sm">{opt.label}</span>
          </div>
        ))}
      </div>

      <div className="flex gap-6 mt-4 text-xs text-gray-600">
        <span><span className="text-blue-400 font-bold">BLINK</span> to cycle</span>
        <span><span className="text-green-400 font-bold">CLENCH</span> to select</span>
        <span><span className="text-red-400 font-bold">TRIPLE</span> to go back</span>
      </div>
    </div>
  )
}
