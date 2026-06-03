import {
  Entity,
  Column,
  PrimaryColumn,
  CreateDateColumn,
  UpdateDateColumn,
  OneToMany,
} from 'typeorm'
import { PriceAlert } from '../price-alert/price-alert.entity'

@Entity('users')
export class User {
  // Telegram chat ID is the primary key
  @PrimaryColumn({ type: 'varchar' })
  chatId: string

  @Column({ type: 'varchar', nullable: true, name: 'first_name' })
  firstName: string

  @Column({ type: 'varchar', nullable: true, name: 'username' })
  username: string

  // Their personal Twelve Data API key
  @Column({ type: 'varchar', nullable: true, name: 'twelve_data_api_key' })
  twelveDataApiKey: string

  // Whether they've redeemed a valid invite code
  @Column({ type: 'boolean', default: false, name: 'is_approved' })
  isApproved: boolean

  // Whether this user is the admin (set via ADMIN_CHAT_ID env var)
  @Column({ type: 'boolean', default: false, name: 'is_admin' })
  isAdmin: boolean

  // Whether they've completed API key setup
  @Column({ type: 'boolean', default: false, name: 'is_setup' })
  isSetup: boolean

  @CreateDateColumn({ name: 'created_at' })
  createdAt: Date

  @UpdateDateColumn({ name: 'updated_at' })
  updatedAt: Date

  @OneToMany(() => PriceAlert, (alert) => alert.user)
  alerts: PriceAlert[]
}
