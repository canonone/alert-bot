import {
  Entity,
  Column,
  PrimaryGeneratedColumn,
  CreateDateColumn,
  ManyToOne,
  JoinColumn,
} from 'typeorm'
import { User } from '../users/user.entity'

export type AlertType = 'SL' | 'TP' | 'TARGET'

@Entity('price_alerts')
export class PriceAlert {
  @PrimaryGeneratedColumn()
  id: number

  // Owner of this alert
  @ManyToOne(() => User, (user) => user.alerts, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'chat_id' })
  user: User

  @Column({ type: 'varchar', name: 'chat_id' })
  chatId: string

  @Column({ type: 'varchar' })
  symbol: string

  @Column({ type: 'varchar' })
  type: AlertType

  @Column({ type: 'decimal', precision: 18, scale: 6, name: 'target_price' })
  targetPrice: number

  @Column({ type: 'boolean', default: true })
  active: boolean

  @CreateDateColumn({ name: 'created_at' })
  createdAt: Date
}
