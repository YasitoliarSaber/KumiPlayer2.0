import type { OpenListConnectionConfig, PublicConfig } from './config'

export function openlistConnections(config: PublicConfig): OpenListConnectionConfig[] {
  return config.openlist_connections || [{ ...config, connection_id: 'legacy', name: config.openlist_connection_name || '默认连接' }]
}

export function selectOpenlistConnection(config: PublicConfig, connectionId: string): PublicConfig {
  const connection = openlistConnections(config).find((c) => c.connection_id === connectionId)
  return connection
    ? { ...config, ...connection, openlist_connection_name: connection.name, openlist_connection_id: connectionId }
    : { ...config, openlist_server_url: '', openlist_remote_root: '/', openlist_mount_root: '', openlist_configured: false, openlist_routes: [], openlist_connection_id: connectionId }
}
