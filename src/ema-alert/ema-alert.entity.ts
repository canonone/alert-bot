import {
  Entity,
  Column,
  PrimaryGeneratedColumn,
  CreateDateColumn,
  ManyToOne,
  JoinColumn,
} from 'typeorm'
import { User } from '../users/user.entity'

@Entity('ema_alerts')
export class EmaAlert {
  @PrimaryGeneratedColumn()
  id: number

  @ManyToOne(() => User, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'chat_id' })
  user: User

  @Column({ type: 'varchar', name: 'chat_id' })
  chatId: string

  @Column({ type: 'varchar' })
  symbol: string

  @Column({ type: 'int', name: 'ema_length', default: 10 })
  emaLength: number

  @Column({ type: 'varchar' })
  timeframe: string

  @Column({ type: 'varchar' })
  direction: string

  @Column({ type: 'int', nullable: false, name: 'user_alert_id', default: 0 })
  userAlertId: number

  @Column({ type: 'boolean', default: true })
  active: boolean

  @CreateDateColumn({ name: 'created_at' })
  createdAt: Date
}
