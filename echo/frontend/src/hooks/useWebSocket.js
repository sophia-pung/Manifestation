import { useEffect, useRef, useCallback, useState } from 'react'

const WS_URL = 'ws://localhost:8765'
const RECONNECT_DELAY = 2000

export function useWebSocket(onMessage) {
  const ws = useRef(null)
  const [connected, setConnected] = useState(false)
  const reconnectTimer = useRef(null)
  const onMessageRef = useRef(onMessage)
  onMessageRef.current = onMessage

  const connect = useCallback(() => {
    if (ws.current && ws.current.readyState < 2) return

    const socket = new WebSocket(WS_URL)

    socket.onopen = () => {
      setConnected(true)
      socket.send(JSON.stringify({ type: 'request_status' }))
    }

    socket.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        onMessageRef.current(msg)
      } catch {}
    }

    socket.onclose = () => {
      setConnected(false)
      reconnectTimer.current = setTimeout(connect, RECONNECT_DELAY)
    }

    socket.onerror = () => {
      socket.close()
    }

    ws.current = socket
  }, [])

  useEffect(() => {
    connect()
    return () => {
      clearTimeout(reconnectTimer.current)
      ws.current?.close()
    }
  }, [connect])

  const send = useCallback((msg) => {
    if (ws.current?.readyState === 1) {
      ws.current.send(JSON.stringify(msg))
    }
  }, [])

  return { connected, send }
}
