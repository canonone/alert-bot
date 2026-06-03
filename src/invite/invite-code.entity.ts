import {
  Entity,
  Column,
  PrimaryGeneratedColumn,
  CreateDateColumn,
} from 'typeorm'

@Entity('invite_codes')
export class InviteCode {
  @PrimaryGeneratedColumn()
  id: number

  @Column({ type: 'varchar', unique: true })
  code: string

  @Column({ type: 'boolean', default: false })
  used: boolean

  // The chat_id of whoever redeemed this code
  @Column({ type: 'varchar', nullable: true, name: 'used_by' })
  usedBy: string

  @Column({ type: 'timestamp', nullable: true, name: 'used_at' })
  usedAt: Date

  @CreateDateColumn({ name: 'created_at' })
  createdAt: Date
}
